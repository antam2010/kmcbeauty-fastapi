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
| 배포(Swarm) | `.env.prod` | `redis://redis:6379/0` | 오버레이 서비스 DNS |

- 배포는 **`.env.prod` 만** 참조한다. 로컬 `.env` 를 배포에 사용하면 오버레이에서
  Redis 해석이 깨진다(결함 재발).
- `.env.prod` 는 저장소에 커밋하지 않는다(`.gitignore` 차단). 템플릿은 `.env.prod.example`.
- 매니저 노드에서 최초 1회:

  ```bash
  cp .env.prod.example .env.prod
  # 비시크릿 값(APP_ENV/ENV/ALGORITHM/토큰 만료/REDIS_URL)만 채운다.
  # SECRET_KEY / FERNET_KEY / DATABASE_URL 은 .env.prod 가 아니라 Docker secret 으로 주입한다(2절).
  ```

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
- CD 파이프라인은 배포 전 이 시크릿들의 존재를 사전 검증한다(7절 참조).

### 2.3 회전 no-op 함정(중요)

Docker secret 은 **불변**이다. 같은 external 이름(`kmc_secret_key_v1`)의 **값만 바꿔서는**
회전되지 않는다(스택 참조 미변경 → 재배포 no-op, 조용한 실패). 반드시 **새 버전 이름**을 만들고
`docker-stack.yml` 의 `source` 를 교체해야 한다. 상세 절차는 9절 회전 runbook 참조.

---

## 3. 불변 이미지 태그 (REQ-INFRA-003)

- 모든 앱 서비스는 `image: ${IMAGE_TAG:-kmcbeauty-api:local}` 로 주입한다.
- 운영/CD 배포는 반드시 **불변 커밋 SHA 태그**를 주입한다:

  ```bash
  IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<git-sha> \
    docker stack deploy -c docker-stack.yml kmcbeauty
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

1. `docker service create --restart-condition=none --secret source=kmc_database_url_v1,target=DATABASE_URL …`
   로 마이그레이션 컨테이너를 띄운다. `alembic/env.py` 가 `app.core.config.settings` 를 import 하므로
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

- 트리거: `pull_request`, `push`
- Python 3.13 + pip 캐시 → `ruff check` → `ruff format --check` → `pytest`(커버리지 리포트)
- 커버리지 게이트(`--cov-fail-under`)는 두지 않는다.
- Redis 서비스 컨테이너 없음(테스트가 전부 모킹).

### CD (`.github/workflows/cd.yml`)

- 트리거: `push` to `main`
- `build-and-push`: GHCR 로그인(GITHUB_TOKEN, `packages: write`) → 불변 SHA 태그 빌드·푸시
- `deploy`: `environment: production`(수동 승인 게이트 가능) → 매니저 노드 SSH →
  **Docker secret 사전 검증(REQ-INFRA-113)** → `deploy_migrate.sh` → `docker stack deploy`

### 필요한 GitHub 저장소 시크릿 (REQ-INFRA-110)

| 시크릿 | 용도 | 소유자 | 회전 |
|---|---|---|---|
| `SWARM_SSH_HOST` | 매니저 노드 호스트/IP | 배포 운영자 | 노드 이전 시 |
| `SWARM_SSH_USER` | 배포 계정(최소 권한) | 배포 운영자 | 계정 변경 시 |
| `SWARM_SSH_KEY` | 배포 계정 개인키 | 배포 운영자 | 9절 회전 runbook |
| `GITHUB_TOKEN` | GHCR 푸시(자동 제공, 별도 등록 불필요) | GitHub | 자동 |

등록/점검은 UI 대신 스크립트를 사용한다(REQ-INFRA-111):

```bash
# 현재 등록 상태 확인(값 미표시)
./scripts/setup_github_secrets.sh --check

# 등록/갱신(idempotent). 개인키는 파일 경로로 주는 것을 권장(히스토리 노출 방지).
SWARM_SSH_HOST=1.2.3.4 SWARM_SSH_USER=deploy \
SWARM_SSH_KEY_FILE=~/.ssh/deploy_key \
  ./scripts/setup_github_secrets.sh
```

사전 준비:

- 매니저 노드에서 GHCR pull 로그인: `echo <PAT> | docker login ghcr.io -u antam2010 --password-stdin`
- 매니저 노드에 저장소 체크아웃(`/opt/kmcbeauty` 등, CD 스크립트의 `DEPLOY_DIR`)
- (선택) GitHub `production` environment 에 필수 리뷰어 설정(수동 승인, REQ-INFRA-010)

### 시크릿 사전 검증 게이트 (REQ-INFRA-113)

`cd.yml` 의 SSH 인라인 스크립트는 `deploy_migrate.sh` 호출 **이전**에 필수 Docker secret
(`kmc_secret_key_v1`/`kmc_fernet_key_v1`/`kmc_database_url_v1`/`firebase_service_account`)의 존재를
`docker secret ls` 로 확인하고, 하나라도 없으면 `exit 1` 로 배포를 중단한다(빈/오설정 대상으로
마이그레이션·배포가 진행되는 것을 방지). `.env.prod` 존재도 함께 확인한다.

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
  docker stack deploy -c docker-stack.yml kmcbeauty
```

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
  docker stack deploy -c docker-stack.yml kmcbeauty

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

