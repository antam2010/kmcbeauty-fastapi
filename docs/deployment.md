# 배포 가이드 (SPEC-INFRA-001 · SPEC-INFRA-002)

KMCBeauty API 의 Docker Swarm 무중단 배포 · CI/CD · 시크릿/마이그레이션/롤백 운영 문서.
시크릿 하드닝(Docker secret 전환 · GitHub 시크릿 자동화 · 회전 runbook)은 SPEC-INFRA-002 참조.

- 오케스트레이션: Docker Swarm 단일 스택(`docker-stack.yml`)
- 레지스트리: GHCR (`ghcr.io/antam2010/kmcbeauty-api:<git-sha>`)
- CI/CD: GitHub Actions (`.github/workflows/ci.yml`, `cd.yml`)
- PostgreSQL 은 컨테이너 외부(관리형/호스트)에 존재하며 이 스택에 포함하지 않는다.

---

## 1. 환경 변수 분리 (REQ-INFRA-001)

| 구분 | 파일 | REDIS_URL | 용도 |
|---|---|---|---|
| 로컬 개발 | `.env` | `redis://localhost:6379/0` | uvicorn/로컬 실행 |
| 배포(Swarm) | `.env.prod` | `redis://kmcbeauty_redis:6379/0` | 스택 전체 서비스명 DNS(`backend` 오버레이) |

- 배포는 **`.env.prod` 만** 참조한다. 로컬 `.env` 를 배포에 사용하면 오버레이에서
  Redis 해석이 깨진다(결함 재발).
- 배포 Redis 는 짧은 이름 `redis` 가 아니라 `kmcbeauty_redis`(`<스택>_<서비스>`)로 접근한다.
  `redis` 는 공유 네트워크의 다른 컨테이너(구 compose 의 `container_name: redis` 등)와 겹칠 수 있다.
- `.env.prod` 는 저장소에 커밋하지 않는다(`.gitignore` 차단). 템플릿은 `.env.prod.example`.
- 매니저 노드에서 최초 1회:

  ```bash
  cp .env.prod.example .env.prod
  # 비시크릿 값(APP_ENV/ENV/ALGORITHM/토큰 만료/REDIS_URL/RATE_LIMIT_STORAGE_URI/
  # FORWARDED_ALLOW_IPS)만 채운다. FORWARDED_ALLOW_IPS 는 템플릿에서 일부러 비워 두었다(10.1절).
  # SECRET_KEY / FERNET_KEY / DATABASE_URL 은 .env.prod 가 아니라 Docker secret 으로 주입한다(2절).
  ```

| 키 | 배포 값 | 이유 |
|---|---|---|
| `RATE_LIMIT_STORAGE_URI` | `redis://kmcbeauty_redis:6379/1` | 로그인 rate limit 카운터를 레플리카 간 공유(기본 `memory://` 는 레플리카별) |
| `FORWARDED_ALLOW_IPS` | `shared_network_prod` 서브넷 CIDR | uvicorn 이 NGINX 의 `X-Forwarded-For` 를 신뢰해 실제 클라이언트 IP 를 복원. 비우면 모든 요청이 NGINX IP 로 보여 한도가 전 사용자 공용. `*` 는 맨 왼쪽(위조 가능) 값을 쓰므로 금지 |

---

## 2. Docker 시크릿 (REQ-INFRA-002 · SPEC-INFRA-002 REQ-INFRA-106/107)

민감 값은 `.env.prod` 평문이 아니라 **Docker secret** 으로만 주입한다. 스택은 각 시크릿을
`kmcbeauty_api`, `celery_worker`, `celery_beat` 세 서비스에 마운트한다(셋 다 `app.core.config`
→ (Firebase 는 `app.core.firebase`) 를 import 시점에 소비하므로 beat 포함 전 서비스에 필요).

### 2.1 시크릿 인벤토리

| external 이름 | target(컨테이너 파일명) | 컨테이너 경로 | 값 | 소유자 |
|---|---|---|---|---|
| `kmc_secret_key_v1` | `SECRET_KEY` | `/run/secrets/SECRET_KEY` | JWT 서명 키 | 배포 운영자 |
| `kmc_fernet_key_v1` | `FERNET_KEY` | `/run/secrets/FERNET_KEY` | Fernet 대칭키(44자) | 배포 운영자 |
| `kmc_database_url_v1` | `DATABASE_URL` | `/run/secrets/DATABASE_URL` | 외부 PostgreSQL 접속 URL | 배포 운영자 |
| `firebase_service_account` | (env 경로 주입) | `/run/secrets/firebase_service_account` | FCM 서비스 계정 키 | 배포 운영자 |

- **핵심 규약**: external 이름은 소문자 + 버전 suffix(`_v1`), `secrets.target` 은 pydantic
  필드명과 정확히 일치(`SECRET_KEY` 등). pydantic-settings 는 `secrets_dir` 파일명이 필드명과
  일치해야 읽으므로 `target` = 필드명이어야 한다.
- **REDIS_URL 은 시크릿이 아니다**(자격 증명 없는 내부 오버레이 DNS) → `.env.prod` 잔류.
  Redis 는 스택 전용 `backend` 오버레이에만 붙어 공유 네트워크에서 보이지 않는다.

### 2.2 최초 등록(매니저 노드, 1회)

값을 저장소나 파일로 남기지 않도록 **stdin** 으로 등록한다(평문 파일 지양):

```bash
# 값을 환경변수로 주고 stdin(-)으로 등록 — 히스토리/파일 노출 최소화
printf %s "$SECRET_KEY"   | docker secret create kmc_secret_key_v1 -
printf %s "$FERNET_KEY"   | docker secret create kmc_fernet_key_v1 -
printf %s "$DATABASE_URL" | docker secret create kmc_database_url_v1 -

# Firebase 키는 파일 → secret (기존과 동일)
docker secret create firebase_service_account ./firebase-service-account.json
```

- 등록 확인: `docker secret ls`.
- CD 파이프라인은 배포 전 이 시크릿들의 존재를 사전 검증한다(6절 참조).

### 2.3 회전 no-op 함정(중요)

Docker secret 은 **불변**이다. 같은 external 이름(`kmc_secret_key_v1`)의 **값만 바꿔서는**
회전되지 않는다(스택 참조 미변경 → 재배포 no-op, 조용한 실패). 반드시 **새 버전 이름**을 만들고
`docker-stack.yml` 의 `source` 를 교체해야 한다. 상세 절차는 9절 회전 runbook 참조.

---

## 3. 불변 이미지 태그 (REQ-INFRA-003)

- 모든 앱 서비스는 `image: ${IMAGE_TAG:-kmcbeauty-api:local}` 로 주입한다.
- 운영/CD 배포는 반드시 **불변 커밋 SHA 태그**를 주입한다(수동 배포도 CD 와 같은 플래그 사용 —
  `--with-registry-auth` 는 매니저의 GHCR 자격 증명을 다른 노드에 전달한다):

  ```bash
  IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<git-sha> \
    docker stack deploy --with-registry-auth -c docker-stack.yml kmcbeauty
  ```

- `latest` 가변 태그는 사용하지 않는다(latest-tag digest 함정 → 롤링 배포 파손).
- `IMAGE_TAG` 미지정 폴백(`kmcbeauty-api:local`)은 단일 노드 로컬 빌드 전용이다.
  멀티 노드/운영에서 IMAGE_TAG 를 누락하면 노드에 이미지가 없어 pull 이 즉시 실패한다.

---

## 4. 헬스체크 기반 롤링 게이팅 (REQ-INFRA-004)

`kmcbeauty_api` 서비스는 서비스 레벨 헬스체크(`curl /health`)와 함께
다음 `update_config` 로 무중단 롤링을 보장한다.

| 키 | 값 | 의미 |
|---|---|---|
| `order` | `start-first` | 새 태스크를 먼저 띄운 뒤 구 태스크 종료 |
| `monitor` | `30s` | 새 태스크가 이 시간 동안 healthy 유지해야 성공 |
| `max_failure_ratio` | `0` | 한 태스크라도 실패하면 롤백 트리거 |
| `failure_action` | `rollback` | 실패 시 직전 정상 태스크로 자동 롤백 |
| `parallelism` | `1` | 한 번에 한 레플리카씩 갱신 |

---

## 5. 마이그레이션 (expand/contract, REQ-INFRA-005)

배포 파이프라인은 스택 업데이트 **이전**에 `alembic upgrade head` 를 실행한다
(`scripts/deploy_migrate.sh`). 무중단 배포 중에는 구/신 레플리카가 동시에 동작하므로
마이그레이션은 **하위 호환(expand/contract)** 을 지켜야 한다.

expand/contract 2단계 규칙(1 릴리스 윈도우):

1. **Expand(릴리스 N)**: 추가만 한다. 새 컬럼/테이블 추가는 nullable 또는 default 포함.
   컬럼 삭제·rename·NOT NULL 강화·타입 축소를 **같은 릴리스에 넣지 않는다**.
2. **코드 이행**: 신버전 코드가 새 스키마를 사용하도록 배포(롤링). 구버전도 여전히 동작.
3. **Contract(릴리스 N+1)**: 모든 레플리카가 신버전이 된 다음 릴리스에서만
   구 컬럼 삭제/rename 등 파괴적 변경을 수행한다.

금지 예시(단일 릴리스에 포함 시 롤링 중 구버전 레플리카가 깨짐):

- 컬럼 `DROP` / `RENAME`
- `NOT NULL` 신규 강제(백필 없이)
- 타입 축소(예: `VARCHAR(255)` → `VARCHAR(50)`)

### 시크릿 주입 방식 (SPEC-INFRA-002 REQ-INFRA-109)

`docker run` 은 Swarm secret 을 마운트하지 못하므로, 컨테이너 경로는 **일회성 Swarm 서비스**로
개편했다(`docker run --env-file` 은 제거). `scripts/deploy_migrate.sh` 는:

1. `docker service create --restart-condition=none --with-registry-auth --constraint node.role==manager --secret source=kmc_database_url_v1,target=DATABASE_URL …`
   로 마이그레이션 컨테이너를 매니저 노드에 띄운다. `alembic/env.py` 가 `app.core.config.settings` 를 import 하므로
   DATABASE_URL 뿐 아니라 필수 필드 `SECRET_KEY`/`FERNET_KEY` 도 시크릿으로 마운트하고,
   비시크릿(`ALGORITHM`/토큰 만료/`ENV`)은 `-e` 로 전달한다.
2. 태스크 상태를 폴링(`docker service ps`)하고 로그를 출력(`docker service logs`)한 뒤 종료 코드를
   확인한다(`docker inspect … ExitCode`). **성공(0)일 때만** 스택 배포로 진행한다(fail-fast).
3. 성공/실패와 무관하게 일회성 서비스를 정리한다(`docker service rm`, trap).

실행:

```bash
# 매니저 노드에서 배포 이미지로 일회성 Swarm 서비스 실행(권장)
IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<sha> ./scripts/deploy_migrate.sh

# 로컬 단일 노드: Docker secret 전환 후 .env.prod 에 DATABASE_URL 이 없으므로
# 운영자가 반드시 DATABASE_URL 을 export 해야 한다(미설정 시 fail-fast).
export DATABASE_URL='postgresql+psycopg://user:pass@host:5432/db'
./scripts/deploy_migrate.sh --local
```

---

## 6. CI/CD (REQ-INFRA-006 ~ 010)

### CI (`.github/workflows/ci.yml`)

- 트리거: `pull_request`, `workflow_call`(CD 가 배포 전에 같은 커밋으로 호출). `main` push 트리거는
  두지 않는다 — CD 가 이미 CI 를 돌리므로 이중 실행이 된다.
- Python 3.13 + pip 캐시 → `ruff check` → `ruff format --check` → `pytest`(커버리지 리포트)
- 커버리지 게이트(`--cov-fail-under`)는 두지 않는다.
- Redis 서비스 컨테이너 없음(테스트가 전부 모킹).
- 동시성: `ci-${{ github.workflow }}-${{ github.ref }}`, 취소는 PR 에서만. 호출될 때 `github` 컨텍스트는
  호출자 것이라 CD 안에서는 `ci-CD-refs/heads/main` 이 되어 PR 실행·CD 그룹(`production-deploy`)과 겹치지 않는다.

### CD (`.github/workflows/cd.yml`)

- 트리거: `push` to `main`. 워크플로 단위 `concurrency: production-deploy`(진행 중 취소 없음) —
  배포는 push 순서대로 하나씩, 대기는 최신 1건만 남는다(구 커밋이 신 커밋을 덮어쓰는 역배포 방지).
- `ci`: `ci.yml` 재사용 호출. 실패하면 이후 잡은 실행되지 않는다.
- `build-and-push`: GHCR 로그인(GITHUB_TOKEN, `packages: write`) → 불변 SHA 태그 빌드·푸시 →
  **푸시한 이미지 스모크**(`--network none` + 더미 env 로 `python -c "import app.main, celery_app"`, 실패 시 배포 안 함)
- `deploy`: `environment: production`(수동 승인 게이트 가능) → 매니저 노드 SSH(호스트 키 지문 검증 선택) →
  **사전 검증**(아래) → `git fetch --prune origin` + `git checkout --force --detach <sha>`(배포 커밋 고정) →
  `docker pull` → `bash scripts/deploy_migrate.sh` → `docker stack deploy --with-registry-auth`

### 필요한 GitHub 저장소 시크릿 (REQ-INFRA-110)

| 시크릿 | 용도 | 소유자 | 회전 |
|---|---|---|---|
| `SWARM_SSH_HOST` | 매니저 노드 호스트/IP | 배포 운영자 | 노드 이전 시 |
| `SWARM_SSH_USER` | 배포 계정(로그인 셸 bash, docker 그룹) | 배포 운영자 | 계정 변경 시 |
| `SWARM_SSH_KEY` | 배포 계정 개인키 | 배포 운영자 | 9절 회전 runbook |
| `SWARM_SSH_FINGERPRINT` | (선택) 호스트 키 지문 `SHA256:…`. 없으면 검증 생략 | 배포 운영자 | 호스트 키 재생성 시 |
| `GITHUB_TOKEN` | GHCR 푸시(자동 제공, 별도 등록 불필요) | GitHub | 자동 |

등록/점검은 UI 대신 스크립트를 사용한다(REQ-INFRA-111):

```bash
# 등록 상태 점검(값 미표시). 필수 시크릿이 없으면 종료 코드 1
./scripts/setup_github_secrets.sh --check

# 등록/갱신(idempotent). 개인키는 파일 경로로 주는 것을 권장(히스토리 노출 방지).
# SWARM_SSH_FINGERPRINT 는 선택(주어졌을 때만 형식 검증 후 등록). 구하는 법은 10.1절.
SWARM_SSH_HOST=1.2.3.4 SWARM_SSH_USER=deploy \
SWARM_SSH_KEY_FILE=~/.ssh/deploy_key \
SWARM_SSH_FINGERPRINT='SHA256:...' \
  ./scripts/setup_github_secrets.sh
```

사전 준비(상세는 10.1절):

- 매니저 노드에서 GHCR pull 로그인: `echo <PAT> | docker login ghcr.io -u antam2010 --password-stdin`
- 매니저 노드에 깨끗한 저장소 clone(`/opt/kmcbeauty`, CD 스크립트의 `DEPLOY_DIR` 기본값)
- (선택) GitHub `production` environment 에 필수 리뷰어 설정(수동 승인, REQ-INFRA-010)

### 사전 검증 게이트 (REQ-INFRA-113 확장)

`cd.yml` 의 SSH 인라인 스크립트는 서버 상태를 바꾸기(체크아웃·마이그레이션·배포) **이전**에 아래를
확인하고, 하나라도 어긋나면 한국어 메시지와 함께 `exit 1` 로 중단한다.

| 검사 | 실패 조건 | 이유 |
|---|---|---|
| Docker secret | `kmc_secret_key_v1`/`kmc_fernet_key_v1`/`kmc_database_url_v1`/`firebase_service_account` 중 누락 | 빈/오설정 대상으로 마이그레이션 방지 |
| `.env.prod` | 파일 없음 | env_file 필수 |
| 시크릿 키 정의 | `DATABASE_URL`/`SECRET_KEY`/`FERNET_KEY` 정의 줄(주석 아님, 빈 값 포함) | env 가 `/run/secrets` 보다 우선해 시크릿을 조용히 덮어씀. 로그에는 줄 번호·키 이름만 출력 |
| `REDIS_URL` | 누락, 또는 `localhost`/`127.0.0.1` 포함 | 컨테이너 안 localhost 는 Redis 아님. 누락 시 기본값 `redis://redis` 는 공유망의 다른 `redis` 로 해석될 수 있음 |
| `RATE_LIMIT_STORAGE_URI` | `localhost`/`127.0.0.1` 포함(누락은 경고) | 위와 동일 |
| `FORWARDED_ALLOW_IPS` | 누락·빈 값, 또는 `*` 포함 | 1절 표 참조. 실제 서브넷과 다르면 경고 |
| `shared_network_prod` | `{{.Driver}} {{.Scope}}` 가 `overlay swarm` 이 아님(없음·이름 중복 포함) | bridge 에는 스택 서비스가 붙지 못함 |

---

## 7. 롤백 (REQ-INFRA-011)

### 자동 롤백

`update_config.failure_action: rollback` + `max_failure_ratio: 0` 에 의해,
새 레플리카가 헬스 게이트를 통과하지 못하면 Swarm 이 직전 정상 태스크로 자동 롤백한다.
`/health` 폴링에 지속적 다운타임이 발생하지 않는다.

### 수동 롤백

```bash
# 방법 A: 직전 스펙으로 서비스 롤백
docker service rollback kmcbeauty_kmcbeauty_api

# 방법 B: 직전 정상 SHA 태그로 재배포(권장, 명시적)
IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<이전-정상-sha> \
  docker stack deploy --with-registry-auth -c docker-stack.yml kmcbeauty
```

- 첫 배포(서비스 신규 생성)에는 되돌아갈 이전 스펙이 없으므로 자동 롤백·`docker service rollback`
  이 동작하지 않는다. 첫 배포의 롤백 기준은 10.3절을 따른다.

상태 확인:

```bash
docker service ps kmcbeauty_kmcbeauty_api          # 태스크 상태/이미지 다이제스트
docker service inspect kmcbeauty_kmcbeauty_api --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'
```

---

## 8. uv 도입 평가 (REQ-INFRA-015, Optional)

결론: **현 시점 미도입, 관찰 유지(revisit)**.

| 항목 | 평가 |
|---|---|
| 이점 | 설치/해상도 속도 대폭 향상, `uv.lock` 재현성, `uv pip` 호환 |
| 비용 | 완전 고정된 `requirements.txt` 트리를 `pyproject`/`uv.lock` 으로 이전하는 마이그레이션 부담, Dockerfile/CI 파이프라인 재작성 |
| 리스크 | 현재 핀 전략(전이 의존까지 수동 고정)이 안정적으로 동작 중 — 교체 시 회귀 위험 |

권고: 의존성 관리 현대화를 별도 SPEC 으로 분리해 진행한다. 도입 시
`pip install -r requirements.txt` → `uv pip sync` 로 1:1 치환부터 시작하고,
`pyproject.toml` 기반 잠금은 그 다음 단계로 미룬다. 이 SPEC 에서는 채택하지 않는다.

---

## 9. 시크릿 회전 runbook (SPEC-INFRA-002 REQ-INFRA-112 / 114)

### 9.1 Docker secret 회전(버전드 external 이름, target 불변)

Docker secret 은 불변이라 **값만 교체하면 재배포가 no-op** 이다(2.3절 함정). 반드시 새 버전
이름을 만들고 스택의 `source` 만 교체한다. `target`(컨테이너 파일명 = 필드명)은 절대 바꾸지 않으므로
**앱 코드는 회전에 무관**하다.

예: `SECRET_KEY` 를 `_v1` → `_v2` 로 회전

```bash
# 1) 새 버전 시크릿 생성(stdin 등록)
printf %s "$NEW_SECRET_KEY" | docker secret create kmc_secret_key_v2 -

# 2) docker-stack.yml 의 source 만 교체(target 은 SECRET_KEY 그대로 유지)
#      - source: kmc_secret_key_v1   →   - source: kmc_secret_key_v2
#        target: SECRET_KEY                target: SECRET_KEY
#    (CD 사전 검증 REQUIRED_SECRETS 목록도 _v2 로 함께 갱신)

# 3) start-first 무중단 롤링 재배포
IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<현재-정상-sha> \
  docker stack deploy --with-registry-auth -c docker-stack.yml kmcbeauty

# 4) 롤아웃 완료(모든 레플리카 _v2 마운트) 확인 후 구 시크릿 제거
docker service ps kmcbeauty_kmcbeauty_api    # 신규 태스크 Running 확인
docker secret rm kmc_secret_key_v1
```

- start-first 라 롤링 중 구 레플리카는 `_v1`, 신 레플리카는 `_v2` 를 각자 마운트하되 둘 다
  `/run/secrets/SECRET_KEY` 로 보이므로 `/health` 폴링 실패 0건(무중단).
- **서명 키(SECRET_KEY) 주의(grace 기간)**: JWT 서명 키를 즉시 교체하면 롤링 도중 구 키로 서명된
  진행 중 토큰이 신 레플리카에서 검증 실패(간헐 401)할 수 있다. 서명 키류는 신·구 키를 동시에
  검증 가능한 grace 기간 전략(예: 다중 키 검증)을 먼저 적용한 뒤 회전한다. 무상태 대칭키
  (`FERNET_KEY`)·접속 URL(`DATABASE_URL`)은 이 제약이 없다.

### 9.2 SSH 배포 키 회전

```bash
# 1) 새 키페어 생성
ssh-keygen -t ed25519 -f ~/.ssh/kmc_deploy_v2 -C "kmc-deploy-rotation"

# 2) 매니저 노드 authorized_keys 에 신규 공개키 추가(구 키는 아직 유지)
ssh-copy-id -i ~/.ssh/kmc_deploy_v2.pub <user>@<manager-host>

# 3) GitHub 시크릿 갱신(개인키 교체) — idempotent
SWARM_SSH_KEY_FILE=~/.ssh/kmc_deploy_v2 ./scripts/setup_github_secrets.sh

# 4) 배포 스모크(main 에 무해한 커밋 또는 수동 워크플로 실행)로 신 키 동작 확인

# 5) 확인 후 매니저 노드 authorized_keys 에서 구 공개키 제거
```

- 구 키 제거는 반드시 신 키 스모크 성공 **이후** 수행한다(잠금 방지).

---

## 10. 첫 Swarm 배포 runbook (compose → Swarm, MySQL → PostgreSQL)

대상은 테스트 서버다. 현재 `main` 의 구 `docker-compose.stage.yml`(api/redis/worker/beat, `.:/app`
바인드 마운트 + `--reload`, MySQL)을 이 저장소의 Swarm 스택으로 교체한다. 이 브랜치의 첫 `main`
병합이 곧 새 CD 의 첫 실행이다.

- 데이터 이관은 하지 않는다. 새 배포는 **빈 PostgreSQL** 에서 시작하고 스키마는 CD 의
  `alembic upgrade head`(단일 baseline `5506c8cf9e2f`)가 만든다.
- 첫 배포는 서비스 **신규 생성**이라 Swarm 자동 롤백이 없다. 구 compose 를 끄기 전까지가 롤백 구간이다.

### 10.1 서버 준비 (병합 전, 매니저 노드)

1. **Swarm·공유 네트워크 확인**

   ```bash
   docker info --format '{{.Swarm.LocalNodeState}}'     # active 가 아니면 docker swarm init
   docker network inspect shared_network_prod -f '{{.Driver}} {{.Scope}} attachable={{.Attachable}}'
   # 기대값: overlay swarm attachable=true → 2번으로
   ```

   `bridge local` 이면 같은 이름의 overlay 를 함께 둘 수 없다(이름 중복도 CD 사전 검증 실패).
   테스트 서버이므로 짧은 중단을 감수하고 교체한다:

   ```bash
   # 서브넷·연결 컨테이너 기록
   docker network inspect shared_network_prod -f '{{range .IPAM.Config}}{{.Subnet}}{{end}} | {{range .Containers}}{{.Name}} {{end}}'
   # 연결된 컨테이너를 모두 분리/중지(구 compose 는 down, NGINX·다른 프로젝트는 disconnect)
   docker network disconnect shared_network_prod <nginx-컨테이너>
   docker network rm shared_network_prod
   docker network create --driver overlay --attachable --subnet <기록한-서브넷> shared_network_prod
   docker network connect shared_network_prod <nginx-컨테이너>   # 다른 프로젝트 컨테이너도 재연결
   ```

   같은 서브넷을 쓰면 구 compose 의 고정 IP(`ipv4_address: 172.20.0.x`)와 IP 기반 NGINX 설정이
   그대로 유효하다. 구 compose 를 다시 올리고(`docker compose -f docker-compose.stage.yml up -d`)
   응답을 확인한 뒤 진행한다.

2. **배포 디렉터리(`DEPLOY_DIR`)는 구 compose 디렉터리와 다른 새 clone**

   ```bash
   sudo install -d -o deploy -g deploy /opt/kmcbeauty
   sudo -u deploy git clone git@github.com:antam2010/kmcbeauty-fastapi.git /opt/kmcbeauty  # private 이면 읽기 전용 deploy key
   ```

   - 구 compose 는 `.:/app` + `uvicorn --reload` 로 작업 트리를 직접 실행한다. 같은 디렉터리면 CD 의
     `git checkout` 이 구 API 를 새 코드(PostgreSQL·Docker secret 전제)로 핫리로드해 NGINX 전환 전에
     서비스가 깨진다. 같은 이유로 구 compose 디렉터리에서는 `git pull` 하지 않는다(이 브랜치는
     `docker-compose.stage.yml` 을 삭제한다).
   - CD 는 `DEPLOY_DIR` 미설정 시 `/opt/kmcbeauty` 를 쓴다. 비대화형 SSH 에서는 `~/.bashrc` 의 export 가
     보통 적용되지 않으므로 기본 경로를 권장한다.

3. **배포 계정**: 로그인 셸 bash(CD 스크립트가 `pipefail` 사용), docker 그룹, GHCR 로그인

   ```bash
   sudo usermod -s /bin/bash -aG docker deploy
   # deploy 계정 셸에서(자격 증명은 ~deploy/.docker/config.json — pull 과 --with-registry-auth 가 사용)
   docker login ghcr.io -u antam2010 --password-stdin    # read:packages 권한 PAT 입력
   ```

4. **Docker secret** (v1 이름 유지 — 첫 배포라 회전 불필요)

   ```bash
   docker secret ls
   # MySQL URL 이 든 예전 kmc_database_url_v1 이 있으면 삭제 후 재생성한다. 값은 조회할 수 없으므로
   # 확실하지 않으면 재생성한다. rm 은 어떤 서비스도 참조하지 않을 때만 성공한다.
   docker secret rm kmc_database_url_v1
   read -rs DATABASE_URL   # postgresql+psycopg://kmcbeauty:<URL-인코딩한-비밀번호>@<pg-host>:5432/kmcbeauty
   printf %s "$DATABASE_URL" | docker secret create kmc_database_url_v1 - && unset DATABASE_URL
   ```

   - 비밀번호 특수문자는 퍼센트 인코딩한다(`@`→`%40`, `:`→`%3A`, `/`→`%2F`, `%`→`%25`).
     `<pg-host>` 는 컨테이너에서 닿는 주소여야 한다(`localhost` 불가).
   - `kmc_secret_key_v1` 은 기존 값을 재사용해도 된다. `kmc_fernet_key_v1`·`firebase_service_account`
     가 없으면 2.2절에 따라 등록한다.

5. **`.env.prod`** (`DEPLOY_DIR` 안. git 미추적이라 CD 체크아웃 후에도 유지된다)

   ```bash
   cd /opt/kmcbeauty && cp .env.prod.example .env.prod
   docker network inspect shared_network_prod -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'   # FORWARDED_ALLOW_IPS 값
   ```

   `APP_ENV=production`, `REDIS_URL=redis://kmcbeauty_redis:6379/0`,
   `RATE_LIMIT_STORAGE_URI=redis://kmcbeauty_redis:6379/1`, `FORWARDED_ALLOW_IPS=<서브넷 CIDR>` 를 확인한다.
   `SECRET_KEY`/`FERNET_KEY`/`DATABASE_URL` 줄은 두지 않는다(CD 가 거부). 구 compose 의 `.env`(MySQL URL)를
   복사하지 않는다.

6. **PostgreSQL** (빈 DB와 role)

   ```sql
   CREATE ROLE kmcbeauty LOGIN PASSWORD '<비밀번호>';
   CREATE DATABASE kmcbeauty OWNER kmcbeauty;
   ```

   - `pg_hba.conf` 에 Swarm 노드 IP 를 허용하고(`host kmcbeauty kmcbeauty <노드-IP>/32 scram-sha-256`)
     `SELECT pg_reload_conf();` 한다. `listen_addresses` 도 그 인터페이스를 포함해야 한다. 컨테이너
     이그레스는 노드 IP 로 SNAT 되지만, PG 가 **같은 호스트**에 있으면 원본 IP 가 `docker_gwbridge` 대역이므로
     그 서브넷을 허용한다(`docker network inspect docker_gwbridge -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'`).
   - 스키마는 만들지 않는다. CD 의 `alembic upgrade head` 가 baseline 부터 생성한다.

7. **GitHub 시크릿** (6절 표). 호스트 키 지문은 선택이지만 권장한다.

   ```bash
   # drone-ssh(Go x/crypto)는 호스트 키를 ECDSA > RSA > Ed25519 순으로 협상한다. 서버에 있는 키 중
   # 가장 앞선 종류의 지문을 쓴다(보통 ECDSA). 출력 두 번째 필드 "SHA256:..." 를 접두사까지 그대로 사용.
   ssh deploy@<manager> 'ls /etc/ssh/ssh_host_*_key.pub; ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub'
   SWARM_SSH_HOST=<manager-ip> SWARM_SSH_USER=deploy SWARM_SSH_KEY_FILE=~/.ssh/kmc_deploy \
   SWARM_SSH_FINGERPRINT='SHA256:...' ./scripts/setup_github_secrets.sh
   ./scripts/setup_github_secrets.sh --check
   ```

   - CD 개인키에 대응하는 공개키를 `~deploy/.ssh/authorized_keys` 에 등록한다.
   - `ssh: host key fingerprint mismatch` 가 나면 키 종류를 잘못 고른 것이다(ssh-action README 예시인
     Ed25519 지문은 ECDSA 키가 있는 서버에서 불일치). 지문 시크릿을 지우면 기존처럼 검증 없이 동작한다.

### 10.2 컷오버 순서

1. **병합** → CD 가 CI → build → 스모크 → 사전 검증 → 마이그레이션 → `stack deploy` 를 수행한다.
   NGINX 는 아직 구 compose 를 가리킨다.
2. **오버레이 내부 헬스체크**

   ```bash
   docker stack services kmcbeauty      # api 2/2, worker 2/2, redis·beat 1/1
   docker run --rm --network shared_network_prod ghcr.io/antam2010/kmcbeauty-api:<sha> \
     curl -fsS http://kmcbeauty_api:8000/health
   docker service logs --tail 50 kmcbeauty_celery_worker   # kmcbeauty_redis 브로커 연결 확인
   ```

3. **NGINX 업스트림 전환** (공유 NGINX 설정 수정 후 `nginx -t && nginx -s reload`)

   ```nginx
   resolver 127.0.0.11 valid=10s ipv6=off;           # Docker 내장 DNS, 요청 시점에 해석
   location / {
       set $kmcbeauty_api http://kmcbeauty_api:8000;  # 변수 proxy_pass
       proxy_pass $kmcbeauty_api;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

   - 고정 `upstream { server kmcbeauty_api:8000; }` 은 기동·리로드 시점에 이름을 해석하므로 스택이 없으면
     공유 NGINX 전체가 실패한다. 변수 방식은 해당 location 만 502 가 된다.
   - 확인: 로그인 6회 연속 시도 시 6번째가 429 이고, `docker service logs kmcbeauty_kmcbeauty_api` 의
     접근 로그 클라이언트 IP 가 서브넷 대역(NGINX)이 아니라 실제 IP 인지 본다(FORWARDED_ALLOW_IPS 정상).
   - 공유망에서 짧은 이름 `kmcbeauty_api` 가 다른 프로젝트와 겹치면 전체 서비스명 `kmcbeauty_kmcbeauty_api` 를 쓴다.

4. **구 compose 중지** (api/worker/beat/redis)

   ```bash
   cd <구-compose-디렉터리>
   docker compose -f docker-compose.stage.yml stop api celery_worker celery_beat redis   # stop: 롤백용 컨테이너 보존
   ```

   - beat 가 둘이면 스케줄 태스크가 중복 디스패치되고(구 beat 는 MySQL 데이터로 계속 동작), 구 redis 는
     호스트 `6379` 를 무인증으로 공개한다. 전환을 확인하면 바로 멈춘다.
   - 안정화되면 `docker compose -f docker-compose.stage.yml down` 으로 정리한다(롤백 구간 종료).

### 10.3 롤백 기준

| 시점 | 조치 |
|---|---|
| NGINX 전환 전(10.2-1~2 실패) | 구 compose 가 계속 서비스하므로 그대로 둔다. `docker service logs` 로 원인을 고쳐 다시 병합한다(필요하면 `docker stack rm kmcbeauty`). |
| NGINX 전환 후(10.2-3~4 이후 문제) | NGINX 업스트림을 이전 값으로 되돌리고 리로드한다. 구 compose 를 멈췄다면 `docker compose -f docker-compose.stage.yml start`. |
| 공통 | 첫 배포는 서비스 신규 생성이라 Swarm 자동 롤백·`docker service rollback` 의 대상(이전 스펙)이 없다. 전환 기간에 PostgreSQL 에 쓰인 데이터는 MySQL 로 돌아가지 않는다(테스트 서버 전제). |

