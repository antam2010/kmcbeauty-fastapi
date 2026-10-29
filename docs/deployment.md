# 배포 가이드 (SPEC-INFRA-001 · SPEC-INFRA-002)

KMCBeauty API 의 운영 배포(단일 서버 · docker compose) · CI/CD · 시크릿/마이그레이션/롤백 운영 문서.
시크릿 하드닝(파일 시크릿 · GitHub 시크릿 자동화 · 회전 runbook)은 SPEC-INFRA-002 참조.

- 운영 서버: 인프라 저장소 **server-mini2** 가 관리하는 서버. 공유 nginx(TLS 종료) · PostgreSQL 18
  (`postgresql`) · Redis(`redis`, 비밀번호 필수)를 다른 앱과 함께 쓴다. 서버에서 Swarm 은 쓰지 않는다
  (2026-10 에 제거) — 앱은 모두 일반 docker compose 프로젝트다.
- 정의: `compose.prod.yaml` (프로젝트 이름 `kmcbeauty`). PostgreSQL · Redis · nginx 는 정의하지 않고 공유
  컨테이너를 쓴다.
- 레지스트리: GHCR (`ghcr.io/antam2010/kmcbeauty-api:<git-sha>`)
- CI/CD: GitHub Actions (`.github/workflows/ci.yml`, `cd.yml`) → SSH 강제 명령 `scripts/deploy_entry.sh`
  → `scripts/deploy.sh`. **`main` 병합 = 운영 배포**(승인 게이트 없음, 리뷰는 PR 에서).
- 공개 주소: `https://api-beauty.dongri.kr` (공유 nginx → `kmcbeauty-api:8000`). nginx 설정 원본은
  `deploy/nginx/api-beauty.conf`.
- 배포 디렉터리(`DEPLOY_DIR`): `/srv/server-mini2/web/kmcbeauty-fastapi` (이 공개 저장소의 https clone).
  `.env.prod` 와 `secrets/` 는 이 디렉터리에만 있고 저장소에는 없다.
- 로컬 개발(`docker/compose/compose.dev.yml`, 프로젝트 `kmcbeauty-dev` + `.env` + Windows 호스트
  PostgreSQL 16)은 이 문서와 별개다. 로컬과 운영은 일부러 다르다.
- **공개 저장소다.** 서버 호스트 이름 · SSH 포트 · 계정 · IP · 서브넷은 저장소에 쓰지 않는다(GitHub 시크릿,
  서버의 `.env.prod` · `secrets/` 에만 둔다). 이 문서의 `<ssh-host>` `<ssh-port>` `<deploy-user>` `<password>` 는
  자리표시자다. Actions 로그도 공개이므로 배포 스크립트는 IP · 서브넷 · 앱 로그(클라이언트 IP)를 출력하지 않는다.

| 서버 구성 | 값 |
|---|---|
| 네트워크 | 외부 bridge `shared_network_prod` (서버 인프라가 관리 — 이 프로젝트는 만들거나 지우지 않는다). nginx 는 고정 IP 로, `postgresql` · `redis` 는 컨테이너 이름으로 닿는다 |
| 컨테이너 | `kmcbeauty-api`(expose 8000, 512m) · `kmcbeauty-celery-worker`(동시성 2, 512m) · `kmcbeauty-celery-beat`(256m). 서비스 이름 = container_name — 공유 네트워크에 `api` 같은 일반 별칭을 만들지 않는다 |
| 호스트 포트 | 없음(`ports:` 없음). 유입은 공유 nginx 뿐 |
| DB | 공유 `postgresql` 의 DB `kmcbeauty` + role `kmcbeauty` (운영자가 생성 — 코드가 만들지 않는다) |
| Redis | 공유 `redis` DB 5(캐시 · 리프레시 토큰 · Celery 브로커/결과) · DB 6(rate limit). `maxmemory 256mb` + `noeviction` 이라 가득 차면 쓰기가 거부된다 |
| 시크릿 | `DEPLOY_DIR/secrets/` 파일 6개(2절) — compose secrets 로 `/run/secrets/<target>` 에 마운트 |
| 백업 | 배포마다 마이그레이션 전에 `pg_dump -Fc` → postgresql 컨테이너의 `/var/lib/postgresql/backup/kmcbeauty-<시각>.dump` |

---

## 1. 환경 변수 분리 (REQ-INFRA-001)

| 구분 | 파일 | Redis | 용도 |
|---|---|---|---|
| 로컬 개발 | `.env` | `REDIS_URL` 평문(compose.dev 가 `redis://redis:6379/0` 으로 덮어씀) | compose.dev · uvicorn |
| 운영 | `.env.prod`(비시크릿만) + `secrets/` 파일 | `secrets/redis_url` = `redis://:<password>@redis:6379/5` | `compose.prod.yaml` |

- 운영은 **`.env.prod` 만** 참조한다(`env_file`). 로컬 `.env` 를 서버에 복사하지 않는다.
- `.env.prod` 는 저장소에 커밋하지 않는다(`.gitignore` 차단). 템플릿은 `.env.prod.example`.
  git 미추적 파일이라 CD 의 `git checkout --force` 뒤에도 남는다.
- `deploy.sh` 가 값을 글자 그대로 비교하므로 한 줄에 `KEY=값` 만 쓴다(따옴표 · 뒤 주석 · `export` · `$` 없이).
- **시크릿 필드(`SECRET_KEY` `FERNET_KEY` `DATABASE_URL` `REDIS_URL` `RATE_LIMIT_STORAGE_URI`)를
  `.env.prod` 에 두지 않는다.** Settings 우선순위가 env > .env > `/run/secrets` 라서 빈 값이라도
  시크릿 파일을 조용히 덮어쓴다. `deploy.sh` 가 배포를 거부한다.

| 키 | 운영 값 | 이유 |
|---|---|---|
| `APP_ENV` | `production` (정확히) | API 문서 비공개 등 운영 동작. `deploy.sh` 가 검사한다 |
| `FORWARDED_ALLOW_IPS` | `shared_network_prod` 에서의 nginx 컨테이너 IP — 서버에서 `docker inspect nginx -f '{{(index .NetworkSettings.Networks "shared_network_prod").IPAddress}}'` 로 본 값 그대로 | uvicorn 이 nginx 의 `X-Forwarded-For` 를 신뢰해 실제 클라이언트 IP 를 복원(rate limit 키). 비우면 모든 요청이 nginx IP 로 보여 한도가 전 사용자 공용, `*` 는 위조 가능한 값을 믿으므로 금지. `deploy.sh` 가 nginx 의 실제 주소와 같은지 비교한다(값은 출력하지 않는다) |
| `RATE_LIMIT_STORAGE_URI` | 시크릿 파일(공유 Redis DB 6) | 로그인 rate limit 카운터를 프로세스 밖에 둔다(기본 `memory://` 는 프로세스별) |

---

## 2. 시크릿 파일 (REQ-INFRA-002 · SPEC-INFRA-002 REQ-INFRA-106/107)

민감 값은 `.env.prod` 평문이 아니라 서버의 **`DEPLOY_DIR/secrets/` 파일**로만 주입한다. `compose.prod.yaml` 의
최상위 `secrets:` 가 `file: ./secrets/<파일>` 로 읽고, 각 서비스가 긴 문법(`source`/`target`)으로 마운트한다.
`target` 은 Settings 필드명과 같아서 앱은 `/run/secrets/<필드명>` 을 그대로 읽는다(pydantic-settings
`secrets_dir`, 값의 앞뒤 공백은 제거된다).

### 2.1 파일 인벤토리

| 파일(`secrets/`) | target(`/run/secrets/…`) | 값 | 마운트 |
|---|---|---|---|
| `secret_key` | `SECRET_KEY` | JWT 서명 키(운영 전용 새 값, `openssl rand -hex 32`) | api · worker · beat |
| `fernet_key` | `FERNET_KEY` | Fernet 키 44자(url-safe base64) — 형식이 틀리면 앱이 import 시점에 죽는다 | api · worker · beat |
| `database_url` | `DATABASE_URL` | `postgresql+psycopg://kmcbeauty:<password>@postgresql:5432/kmcbeauty` | api · worker · beat |
| `redis_url` | `REDIS_URL` | `redis://:<password>@redis:6379/5` | api · worker · beat |
| `rate_limit_storage_uri` | `RATE_LIMIT_STORAGE_URI` | `redis://:<password>@redis:6379/6` | api |
| `firebase_service_account.json` | `firebase_service_account` | FCM 서비스 계정 키 JSON. 실제 키 전에는 `{}` | api · worker · beat |

- 마이그레이션(`docker compose run … kmcbeauty-api`)도 api 와 같은 시크릿을 받는다.
- Redis URL 도 시크릿이다. 공유 Redis 는 비밀번호가 필수라 URL 에 자격 증명이 들어간다.
- Firebase 키 경로(`FIREBASE_SERVICE_ACCOUNT_KEY_PATH=/run/secrets/firebase_service_account`)는
  `compose.prod.yaml` 이 고정한다. `{}` 이면 Firebase 초기화만 실패(오류 로그)하고 API · worker · beat 는
  정상 동작한다. 푸시 발송은 실제 키로 바꿀 때까지 실패한다(9절).
- `scripts/deploy.sh` 의 `SECRET_FILES` 가 이 6개를 사전 검사한다. compose 의 파일 목록과 다르면 가드 테스트
  (`tests/test_infra_guards.py`)가 실패한다.

### 2.2 권한 · 소유자

| 대상 | 권한 | 이유 |
|---|---|---|
| `secrets/` 디렉터리 | `0700`, 배포 계정 소유 | 다른 호스트 사용자가 파일에 닿지 못한다 |
| 파일 6개 | `0644`, 비어 있지 않음, 끝 줄바꿈 없음(printf) | compose(비 Swarm)의 파일 시크릿은 읽기 전용 **bind mount** 라 호스트 권한이 그대로 보인다. 컨테이너는 이미지의 `appuser`(시스템 uid — 배포 계정 uid 와 다름)로 돌므로 0600 이면 읽지 못한다 |

`deploy.sh` 가 디렉터리 0700 · 소유자, 파일 존재 · 비어 있지 않음 · 0644 를 검사한다.

### 2.3 파일을 바꾼 뒤에는 다시 만든다(중요)

앱은 시크릿을 **시작할 때 한 번** 읽는다. 또 파일을 새로 만들면(편집기 · `mv` · `cp` 처럼 inode 가 바뀌면)
실행 중인 컨테이너는 옛 파일을 계속 본다. 같은 태그로 `deploy.sh` 를 다시 돌려도 compose 는 바뀐 설정이
없어 컨테이너를 그대로 둔다. 그래서 시크릿을 바꾸면 반드시 `up -d --force-recreate` 한다(9절).

---

## 3. 불변 이미지 태그 (REQ-INFRA-003)

- 세 서비스 모두 `image: ${IMAGE_TAG:?...}` 다. `IMAGE_TAG` 가 없거나 비면 `docker compose config/up` 이
  즉시 실패한다(기본값 없음).
  - 오류 메시지에 `-` 를 넣지 않는다. docker CLI(stack)의 치환은 `-`(기본값) 규칙을 `:?` 보다 먼저 적용해
    `${IMAGE_TAG:?a-b}` 를 기본값 `b` 로 조용히 바꾼다(compose v2 는 영향 없음 — 가드 테스트가 검사).
- 운영 배포는 `deploy.sh` 가 `IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<40자 sha>` 를 export 하고
  다음을 실행한다:

  ```bash
  docker compose -p kmcbeauty -f compose.prod.yaml up -d --remove-orphans --wait --wait-timeout 180
  ```

  - `deploy.sh` 가 먼저 pull 해 두므로 compose 는 로컬 이미지를 쓴다. 레지스트리 토큰은 pull 하는 동안만
    임시 `DOCKER_CONFIG` 에 있다가 지워진다.
  - `--remove-orphans`: `kmcbeauty` 프로젝트에서 빠진 서비스의 컨테이너만 지운다. 다른 프로젝트 · 공유
    컨테이너는 건드리지 않는다.
- `latest` 가변 태그는 사용하지 않는다(latest-tag digest 함정).

---

## 4. 기동 · 헬스체크 (REQ-INFRA-004)

- compose 는 설정(이미지 · env · 시크릿 정의 등)이 바뀐 컨테이너를 **멈추고 지운 뒤 다시 만든다**(롤링 없음).
  api 는 재생성 동안 **잠깐(수 초) 응답하지 않는다** — nginx 는 이 동안 502 를 돌려준다. 1 컨테이너 ·
  고정 container_name 이라 무중단 롤링은 하지 않는다.
- `up --wait` 는 api 가 `healthy`(이미지의 HEALTHCHECK: `curl -fsS http://localhost:8000/health`,
  interval 30s · start-period 20s · retries 3 — 보통 30초 남짓), worker · beat 가 `running` 이 될 때까지
  기다린다(최대 180초). celery 는 웹서버가 아니라 이미지 헬스체크를 끈다.
- `deploy.sh` 는 그 뒤 api `healthy`, 세 컨테이너 `running`, 모두 이번 이미지인지 다시 확인한다.
- `restart: unless-stopped` — 서버 재부팅 · 프로세스 종료 시 docker 가 다시 띄운다(`down` · `stop` 한 것은 제외).
- 메모리 상한 `mem_limit` 512m(api) · 512m(worker) · 256m(beat). 로그는 json-file 10m × 3 으로 회전한다
  (서버 인프라의 다른 서비스와 같은 값).
- 자동 롤백은 없다. 실패하면 `deploy.sh` 가 출력하는 롤백 명령을 실행한다(7절).

---

## 5. 마이그레이션 (expand/contract, REQ-INFRA-005)

배포 파이프라인은 컨테이너를 바꾸기 **이전**에 `alembic upgrade head` 를 실행한다(`deploy.sh`, pg_dump 백업
직후). 새 컨테이너가 뜨기 전 잠깐 동안 구버전 api 가 새 스키마에서 돌고, 이미지만 되돌리는 롤백은 스키마를
그대로 두므로 마이그레이션은 **하위 호환(expand/contract)** 을 지켜야 한다.

expand/contract 2단계 규칙(1 릴리스 윈도우):

1. **Expand(릴리스 N)**: 추가만 한다. 새 컬럼/테이블 추가는 nullable 또는 default 포함.
   컬럼 삭제·rename·NOT NULL 강화·타입 축소를 **같은 릴리스에 넣지 않는다**.
2. **코드 이행**: 신버전 코드가 새 스키마를 사용하도록 배포. 구버전도 여전히 동작.
3. **Contract(릴리스 N+1)**: 신버전이 자리 잡은 다음 릴리스에서만
   구 컬럼 삭제/rename 등 파괴적 변경을 수행한다.

금지 예시(단일 릴리스에 포함 시 구버전 코드 · 이미지 롤백이 깨짐):

- 컬럼 `DROP` / `RENAME`
- `NOT NULL` 신규 강제(백필 없이)
- 타입 축소(예: `VARCHAR(255)` → `VARCHAR(50)`)

**운영 DB 에는 `upgrade` 만 한다.** downgrade 경로는 두지 않는다. 롤백은 이미지 · env 만 되돌리고
스키마는 그대로 둔다(7절). PostgreSQL 은 DDL 이 트랜잭션이라 upgrade 가 실패하면 스키마가 바뀌지 않는다.

### 실행 방식

새 이미지의 일회성 컨테이너로 실행한다. api 서비스 정의(시크릿 · `.env.prod` · `shared_network_prod`)를 그대로
쓰므로 `DATABASE_URL` 의 호스트 `postgresql` 이 그대로 해석된다. 실행 중인 `kmcbeauty-api` 와 이름이 겹치지 않는다
(compose 가 `kmcbeauty-kmcbeauty-api-run-<id>` 로 만든다).

```bash
# 서버(배포 디렉터리)에서 — 보통은 deploy.sh 가 실행한다
export IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<sha>
docker compose -p kmcbeauty -f compose.prod.yaml run --rm --no-deps -T kmcbeauty-api alembic upgrade head
docker compose -p kmcbeauty -f compose.prod.yaml run --rm --no-deps -T kmcbeauty-api alembic current   # "(head)" 포함
```

`deploy.sh` 는 `alembic current` 출력에 `(head)` 가 없으면 종료 코드 4 로 멈추고 컨테이너를 바꾸지 않는다.

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
- `build-and-push`: GHCR 로그인(GITHUB_TOKEN, `packages: write`) → 불변 SHA 태그 빌드·푸시
  (`org.opencontainers.image.source` 라벨로 패키지를 이 저장소에 연결) →
  **푸시한 이미지 스모크**(`--network none` + 더미 env 로 `python -c "import app.main, celery_app"`, 실패 시 배포 안 함)
- `deploy`: `environment: production`(배포 이력용 — 필수 리뷰어를 두지 않는다), `permissions: contents: read,
  packages: read`, `timeout-minutes: 20`.
  1. 저장소 시크릿 5개 중 하나라도 비었으면 이름을 출력하고 즉시 실패한다.
  2. 개인키 · known_hosts 를 러너 임시 디렉터리에 쓰고(600) 러너의 **OpenSSH** 로 접속한다(서드파티 SSH
     액션 없음): `BatchMode=yes`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=<시크릿>`,
     `GlobalKnownHostsFile=/dev/null`, `-p <포트>`.
  3. 원격 명령은 `deploy <sha>` 하나뿐이고, stdin 첫 줄로 잡의 `GITHUB_TOKEN` 을 넘긴다(서버의 GHCR pull 용,
     잡이 끝나면 만료).

### 서버 쪽 흐름

`scripts/deploy_entry.sh` — CI 키에 묶인 sshd 강제 명령(10절 (e)):

1. `$SSH_ORIGINAL_COMMAND` 가 정확히 `deploy <40자 소문자 hex>` 가 아니면 git · docker · 파일을 건드리기
   **전에** 종료 코드 2 로 거부한다(정규식 검사만, 실행 · eval 없음).
2. stdin 첫 줄의 토큰 형식을 확인하고 `REGISTRY_TOKEN` · `REGISTRY_USER=antam2010` 로 넘긴다.
3. 잠금(`.git/kmcbeauty-deploy.lock`, flock) → `git fetch --prune origin main` → 그 sha 가 `origin/main` 의
   조상인지 확인(아니면 2) → `git checkout --force --detach <sha>` → **그 커밋의** `scripts/deploy.sh <sha>` 를
   exec 한다. 잠금은 deploy.sh 가 끝날 때까지 유지된다(수동 배포와도 겹치지 않는다).

`scripts/deploy.sh <sha>` — 서버에서 배포 계정으로 실행(수동 배포도 같은 스크립트):

| 단계 | 내용 · 실패 조건 |
|---|---|
| 전제: compose | `docker compose version` 실패 |
| 전제: 네트워크 | `shared_network_prod` 없음(이 스크립트는 만들지 않는다) |
| 전제: nginx | `nginx` 컨테이너가 없거나 `shared_network_prod` 에 붙어 있지 않음 |
| 전제: `.env.prod` | 파일 없음, `APP_ENV` 가 정확히 `production` 아님, 시크릿 필드 정의 줄(주석 아님 · 빈 값 포함 — 줄 번호만 출력), `FORWARDED_ALLOW_IPS` 비었음 · `*` 포함 · nginx 의 `shared_network_prod` 주소와 다름(`docker inspect` 로 읽고 출력하지 않는다) |
| 전제: 시크릿 | `secrets/` 가 0700 · 배포 계정 소유가 아님, 파일 6개 중 없음 · 빈 파일 · 0644 아님 |
| 전제: 커밋 | `git rev-parse HEAD` 가 요청 sha 와 다름, 추적 파일 수정됨 |
| 전제: 렌더 | `docker compose -p kmcbeauty -f compose.prod.yaml config -q` 실패 |
| 이미지 | `REGISTRY_TOKEN` 이 있으면 임시 `DOCKER_CONFIG` 로 login → pull → logout(배포 계정의 기존 docker 로그인은 건드리지 않는다). pull 이 실패해도 같은 태그가 서버에 있으면 그것을 쓴다 |
| 직전 이미지 | 돌고 있던 `kmcbeauty-api` 의 이미지를 기록(롤백 안내용) |
| 백업 | `docker exec postgresql pg_dump -U postgres -d kmcbeauty -Fc -f /var/lib/postgresql/backup/kmcbeauty-<시각>.dump` + `pg_restore -l` 검증. 실패하면 중단 |
| 마이그레이션 | 5절. 실패하거나 `(head)` 가 아니면 컨테이너는 그대로 |
| 기동 | `up -d --remove-orphans --wait --wait-timeout 180` → api `healthy` · 세 컨테이너 `running` · 모두 이번 이미지 |
| 스모크 | `docker exec nginx wget -qO- -T 10 http://kmcbeauty-api:8000/health`, 이어서 외부 `curl -fsS https://api-beauty.dongri.kr/health` (`SMOKE_URL` 로 변경, 빈 값이면 생략). 각 6회까지 재시도 |
| 요약 | 컨테이너 · 이미지 · 백업 파일, 롤백 명령(직전 sha · 직전 이미지) |

종료 코드(deploy.sh — 강제 명령을 거쳐 CD 잡의 종료 코드가 된다):

| 코드 | 의미 | 서버 상태 |
|---|---|---|
| 0 | 성공 | 새 버전 |
| 2 | 요청 거부(deploy_entry) · 전제 실패 | 컨테이너 그대로(deploy_entry 가 체크아웃까지 했을 수는 있다) |
| 3 | 이미지 pull 실패 | 컨테이너 그대로 |
| 4 | 백업 · 마이그레이션 실패 | 컨테이너 그대로(백업 파일은 남을 수 있다) |
| 5 | 기동 실패(`up --wait` · 상태 확인) | **새 컨테이너가 비정상이거나 내려가 있을 수 있다 — 자동 롤백 없음.** 출력된 롤백 명령 실행 |
| 6 | 스모크 실패 | 새 버전이 떠 있음 — 원인 확인 후 롤백 판단 |

### 필요한 GitHub 저장소 시크릿 (REQ-INFRA-110)

| 시크릿 | 용도 | 소유자 | 회전 |
|---|---|---|---|
| `DEPLOY_SSH_HOST` | 서버 SSH 호스트 — Cloudflare 프록시를 거치지 않는 이름/IP(인터넷에서 닿아야 함) | 배포 운영자 | 주소 변경 시 |
| `DEPLOY_SSH_PORT` | 서버 SSH 포트 | 배포 운영자 | 포트 변경 시 |
| `DEPLOY_SSH_USER` | 배포 계정(docker 그룹, `DEPLOY_DIR` 소유, 로그인 셸 bash) | 배포 운영자 | 계정 변경 시 |
| `DEPLOY_SSH_KEY` | CI 전용 개인키(암호 없음). 서버에서 강제 명령에만 묶인다 | 배포 운영자 | 9.2절 |
| `DEPLOY_SSH_KNOWN_HOSTS` | 서버 호스트 키(known_hosts 형식, 포트가 22 가 아니면 `[host]:port …`) | 배포 운영자 | 호스트 키 재생성 시 |
| `GITHUB_TOKEN` | GHCR 푸시(build) · 서버의 pull(deploy, stdin 으로 전달). 자동 발급 · 잡 종료 시 만료 | GitHub | 자동 |

등록/점검은 UI 대신 스크립트를 사용한다(REQ-INFRA-111). 상세는 10절 (f).

```bash
# 등록 상태 점검(값 미표시). 필수 시크릿이 없으면 종료 코드 1
./scripts/setup_github_secrets.sh --check

# 등록/갱신(idempotent). 개인키 · known_hosts 는 파일 경로로 준다(값이 히스토리에 남지 않는다).
DEPLOY_SSH_HOST=<ssh-host> DEPLOY_SSH_PORT=<ssh-port> DEPLOY_SSH_USER=<deploy-user> \
DEPLOY_SSH_KEY_FILE=~/.ssh/kmcbeauty_cd DEPLOY_SSH_KNOWN_HOSTS_FILE=~/.ssh/kmcbeauty_known_hosts \
  ./scripts/setup_github_secrets.sh
```

예전 방식의 시크릿(`SWARM_SSH_HOST` · `SWARM_SSH_USER` · `SWARM_SSH_KEY` · `SWARM_SSH_FINGERPRINT`)은 더 쓰지
않는다. 남아 있으면 지운다: `gh secret delete SWARM_SSH_HOST` (나머지도 같은 방식).

---

## 7. 롤백 (REQ-INFRA-011)

compose 에는 자동 롤백이 없다. `deploy.sh` 는 배포 전에 돌던 `kmcbeauty-api` 의 이미지를 기록해 두고, 실패(5 · 6)와
성공 요약에서 그대로 실행할 수 있는 롤백 명령을 출력한다. 배포 디렉터리에서 실행한다.

```bash
cd /srv/server-mini2/web/kmcbeauty-fastapi

# 방법 A: 이미지만 직전 것으로(가장 빠름 — 지금 체크아웃의 compose.prod.yaml · .env.prod · secrets/ 를 그대로 쓴다)
IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<직전-sha> docker compose -p kmcbeauty -f compose.prod.yaml up -d --wait

# 방법 B: 직전 커밋으로 다시 배포(백업 · 마이그레이션 · 스모크까지 같은 절차)
git fetch --prune origin main && git checkout --detach <직전-sha>
scripts/deploy.sh <직전-sha>
```

- 둘 다 **스키마는 되돌리지 않는다**(expand/contract 전제, 5절).
- 방법 B 는 그 사이 새 마이그레이션이 없을 때만 된다. 있으면 구 코드의 alembic 이 DB 의 revision 을 몰라
  마이그레이션 단계에서 종료 코드 4 로 멈춘다(컨테이너는 그대로) — 이때는 방법 A 를 쓴다.
- 첫 배포는 직전 컨테이너가 없다. 내려야 하면 `docker compose -p kmcbeauty down`(또는 `scripts/stop.sh`) —
  외부 네트워크 · `secrets/` · DB · Redis 는 남는다.
- 다음 CD(main 병합)는 다시 최신 커밋을 배포한다. 문제 커밋은 revert 해서 병합한다.

상태 확인:

```bash
docker compose -p kmcbeauty ps
docker inspect kmcbeauty-api -f '{{.Config.Image}} {{.State.Health.Status}}'
docker logs --tail 50 kmcbeauty-api        # 서버에서만 본다(접근 로그에 클라이언트 IP 가 있다)
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

### 9.1 시크릿 파일 교체

모든 시크릿은 같은 절차다: **파일을 바꾸고 → 지금 이미지 그대로 `--force-recreate`**(2.3절 — 앱은 시작할 때만
읽는다). 저장소 · 이미지 · CD 는 건드리지 않는다. 재생성하는 동안 api 가 잠깐 응답하지 않는다.

```bash
cd /srv/server-mini2/web/kmcbeauty-fastapi
umask 022
# 1) 값 교체(끝 줄바꿈 없이). 기존 파일에 덮어쓰면 권한 0644 가 유지된다.
printf '%s' "$(openssl rand -hex 32)" > secrets/secret_key
stat -c '%a %n' secrets/*        # 0644 인지 확인

# 2) 지금 돌고 있는 이미지로 세 컨테이너를 다시 만든다
IMAGE_TAG="$(docker inspect kmcbeauty-api -f '{{.Config.Image}}')" \
  docker compose -p kmcbeauty -f compose.prod.yaml up -d --force-recreate --wait
```

| 시크릿 | 교체 시 주의 |
|---|---|
| `secret_key` | JWT 서명 키라 교체 즉시 **기존 토큰이 모두 무효**다(전 사용자 재로그인). 무중단 교체가 필요하면 신·구 키를 함께 검증하는 grace 기간 전략을 먼저 넣는다 |
| `fernet_key` | 확인 결과(`app/core/security.py`): 앱은 시작 시 `Fernet(FERNET_KEY)` 를 만들고 `encrypt_token` / `decrypt_token` 을 정의하지만 **호출하는 곳이 없다 — 현재 이 키로 암호화해 저장하는 데이터는 없다.** 그래서 지금은 교체해도 기존 데이터가 깨지지 않는다. 다만 이 함수로 저장 데이터를 암호화하기 시작하면, 키를 바꾸는 순간 **기존 암호문은 복호화할 수 없게 된다**(키 목록 · 재암호화가 없다). 형식이 틀린 값이면 앱이 import 시점에 죽는다 |
| `database_url` | 먼저 `docker exec -it postgresql psql -U postgres` → `\password kmcbeauty` 로 비밀번호를 바꾸고, 같은 값으로 파일을 쓴 뒤 재생성한다. 그 사이 새 연결은 인증에 실패한다 |
| `redis_url` · `rate_limit_storage_uri` | server-mini2 의 `REDIS_PASSWORD` 가 바뀌면 두 파일을 함께 바꾼다 |
| `firebase_service_account.json` | 지금은 `{}` 자리표시자로 둔다(10절 (c)). 실제 키가 생기면 서버로 복사해 바꾸고 재생성한다 — 아래 |

Firebase 실제 키로 바꾸기:

```bash
# 운영자 PC → 서버(배포 계정 홈) 로 키 JSON 복사 후, 서버의 배포 디렉터리에서:
install -m 644 ~/firebase-service-account.json secrets/firebase_service_account.json \
  && shred -u ~/firebase-service-account.json
IMAGE_TAG="$(docker inspect kmcbeauty-api -f '{{.Config.Image}}')" \
  docker compose -p kmcbeauty -f compose.prod.yaml up -d --force-recreate --wait
docker logs --since 5m kmcbeauty-api 2>&1 | grep -i firebase   # "Firebase Admin SDK initialized successfully"
```

### 9.2 SSH 배포 키 회전

```bash
# 1) 운영자 PC: 새 키페어(암호 없음, CI 전용)
ssh-keygen -t ed25519 -N '' -C kmcbeauty-cd -f ~/.ssh/kmcbeauty_cd_v2

# 2) 서버: 배포 계정 ~/.ssh/authorized_keys 에 새 강제 명령 줄 추가(구 줄은 아직 유지)
#    restrict,command="/srv/server-mini2/web/kmcbeauty-fastapi/scripts/deploy_entry.sh" ssh-ed25519 AAAA... kmcbeauty-cd

# 3) GitHub 시크릿 갱신(idempotent — 다섯 값 모두 다시 준다)
DEPLOY_SSH_HOST=<ssh-host> DEPLOY_SSH_PORT=<ssh-port> DEPLOY_SSH_USER=<deploy-user> \
DEPLOY_SSH_KEY_FILE=~/.ssh/kmcbeauty_cd_v2 DEPLOY_SSH_KNOWN_HOSTS_FILE=~/.ssh/kmcbeauty_known_hosts \
  ./scripts/setup_github_secrets.sh

# 4) 신 키 확인: 마지막 CD 실행을 Actions 에서 다시 실행(같은 sha 재배포 — 바뀐 것이 없으면 컨테이너 그대로)

# 5) 확인 후 서버 authorized_keys 에서 구 키 줄 제거
```

- 구 키 제거는 반드시 신 키 확인 **이후** 수행한다(잠금 방지).

---

## 10. 첫 배포 runbook (운영 서버)

- 대상: server-mini2 가 관리하는 서버. 다른 앱도 compose 프로젝트로 `shared_network_prod` 에 붙어 돈다.
  이 절차는 compose 프로젝트 `kmcbeauty`, `DEPLOY_DIR` 의 `.env.prod` · `secrets/`, DB `kmcbeauty`, Redis DB 5 · 6 만
  만든다.
- **공유 자원은 만들거나 지우지 않는다**: `shared_network_prod`, nginx, postgresql, redis.
- 데이터: **빈 PostgreSQL DB** 에서 시작한다. 스키마는 CD 의 `alembic upgrade head`(단일 baseline
  `5506c8cf9e2f`)가 만든다. 기존 MySQL 데이터 이관은 별도 작업이다(이 runbook 범위 밖).
- 순서: (a)~(h) 를 병합 **전에** 끝내고 (i) 에서 병합한다. (g) 의 nginx 설정은 변수 proxy_pass 라 컨테이너가
  없어도 리로드된다(이 호스트만 502) — 먼저 넣어 둔다.

사전 확인(서버, 읽기 전용):

```bash
docker compose version
docker network inspect shared_network_prod -f '{{.Driver}}'                     # bridge
docker network inspect shared_network_prod -f '{{range .Containers}}{{.Name}} {{end}}'   # nginx · postgresql · redis 포함
```

### (a) 배포 디렉터리 clone

```bash
# 배포 계정으로(docker 그룹 · 로그인 셸 bash). 공개 저장소라 https clone 에 자격 증명이 필요 없다.
id -nG | tr ' ' '\n' | grep -x docker
git clone https://github.com/antam2010/kmcbeauty-fastapi.git /srv/server-mini2/web/kmcbeauty-fastapi
cd /srv/server-mini2/web/kmcbeauty-fastapi
```

- 이미 clone 이 있으면 생략한다. 이 디렉터리가 `DEPLOY_DIR` 이고 (e) 의 강제 명령도 이 안의
  `scripts/deploy_entry.sh` 를 가리킨다. 위치를 바꾸면 deploy_entry.sh 의 기본값과 command 경로를 함께 바꾼다.
- 작업 트리는 CD 가 `git checkout --force --detach <sha>` 로 바꾼다. 여기서 추적 파일을 고치지 않는다
  (`.env.prod` · `secrets/` 만 둔다 — 둘 다 git 미추적).

### (b) PostgreSQL role · DB

비밀번호는 서버에서 만든다(hex 라 URL 인코딩이 필요 없다). (c) 에서 같은 셸의 `$PGPW` 를 쓰므로 셸을 닫지 않는다.

```bash
PGPW="$(openssl rand -hex 24)"
printf '%s\n' "$PGPW"     # 한 번만 보이고 아래 \password 에 붙여 넣는다(끝나면 clear)
docker exec -it postgresql psql -U postgres
```

```sql
\du kmcbeauty
-- role 이 없으면 만든다. 이미 있으면 이 줄은 건너뛰고(LOGIN 이 없으면 ALTER ROLE kmcbeauty LOGIN;)
CREATE ROLE kmcbeauty LOGIN;
-- 있든 없든 비밀번호를 지금 만든 값으로 둔다(= ALTER ROLE … PASSWORD, 서버에는 해시만 전달된다)
\password kmcbeauty
\l kmcbeauty
-- DB 가 없으면 만든다. 이미 있으면 ALTER DATABASE kmcbeauty OWNER TO kmcbeauty;
CREATE DATABASE kmcbeauty OWNER kmcbeauty;
-- (선택) 다른 앱 role 의 접속 차단 — 1 앱 1 DB 규약
REVOKE CONNECT ON DATABASE kmcbeauty FROM PUBLIC;
\q
```

- DB 소유자라 `public` 스키마에 테이블 · enum 을 만들 수 있다(baseline 은 확장이 필요 없다).
  스키마는 만들지 않는다 — CD 의 마이그레이션이 만든다.
- 컨테이너는 `shared_network_prod` 에서 `postgresql:5432` 로 접속한다(다른 compose 앱과 같은 경로).
  (i) 의 마이그레이션이 `no pg_hba.conf entry` 로 실패하면 server-mini2 의 PostgreSQL 설정을 확인한다.

### (c) 시크릿 파일 6개

```bash
cd /srv/server-mini2/web/kmcbeauty-fastapi
install -d -m 700 secrets          # 배포 계정 소유 · 0700
umask 022                          # 새 파일 0644

# 값은 서버에서 만들고 printf 로 끝 줄바꿈 없이 쓴다(화면에 출력하지 않는다)
printf '%s' "$(openssl rand -hex 32)" > secrets/secret_key
printf '%s' "$(openssl rand -base64 32 | tr '+/' '-_')" > secrets/fernet_key
printf 'postgresql+psycopg://kmcbeauty:%s@postgresql:5432/kmcbeauty' "$PGPW" > secrets/database_url && unset PGPW

# Redis — server-mini2 의 REDIS_PASSWORD(redis 컨테이너 환경변수). DB 5 · 6 이 비어 있는지 먼저 확인한다.
docker exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning INFO keyspace'   # db5 · db6 줄이 없어야 한다
REDIS_PW="$(docker exec redis printenv REDIS_PASSWORD)"
# URL 특수문자(@ : / % # ? 등)를 퍼센트 인코딩한다 — 값은 stdin 으로만 넘긴다
REDIS_PW_URL="$(printf '%s' "$REDIS_PW" | python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read(), safe=""))')"
printf 'redis://:%s@redis:6379/5' "$REDIS_PW_URL" > secrets/redis_url
printf 'redis://:%s@redis:6379/6' "$REDIS_PW_URL" > secrets/rate_limit_storage_uri
unset REDIS_PW REDIS_PW_URL

# Firebase — 실제 키가 생길 때까지 자리표시자(9.1절에서 실제 키로 바꾼다)
printf '{}' > secrets/firebase_service_account.json

chmod 700 secrets && chmod 644 secrets/*
ls -la secrets       # drwx------ <deploy-user> · -rw-r--r-- × 6 (크기 0 인 파일이 없어야 한다)
```

- `{}` 로 시작하면 Firebase 초기화만 실패(오류 로그)하고 API · worker · beat 는 정상 동작한다.
  **푸시 발송은 실제 키로 바꿀 때까지 실패한다.**
- `INFO keyspace` 에 db5 · db6 이 이미 있으면 다른 앱이 쓰는 중이다. 멈추고 번호를 다시 정한다
  (두 파일 · server-mini2 `docs/PORTS.md` 를 함께 바꾼다).
- 시크릿 값은 이 디렉터리에만 있다. 백업이 필요하면 서버의 비밀 보관 방식에 맞춰 따로 둔다(저장소 금지).

### (d) `.env.prod`

```bash
cp .env.prod.example .env.prod && chmod 600 .env.prod
docker inspect nginx -f '{{(index .NetworkSettings.Networks "shared_network_prod").IPAddress}}'
# 출력값을 FORWARDED_ALLOW_IPS= 뒤에 그대로(따옴표 · 공백 없이) 넣어 자리표시자를 바꾼다
grep -nE '^[[:space:]]*(export[[:space:]]+)?(SECRET_KEY|FERNET_KEY|DATABASE_URL|REDIS_URL|RATE_LIMIT_STORAGE_URI)' .env.prod   # 출력 없어야 한다
```

- `APP_ENV=production` 을 확인한다. 로컬 `.env` 나 예전 MySQL 시절 `.env` 를 복사하지 않는다.
- nginx 는 `shared_network_prod` 에 고정 IP 로 붙어 있다. 그 IP 가 바뀌면(서버 인프라 변경) 이 값도 바꾼다 —
  다르면 `deploy.sh` 가 배포를 막는다.

### (e) CI 전용 키 · 강제 명령

```bash
# 운영자 PC(서버 아님): 이 용도 전용 키. CI 는 비대화형이라 암호 없음.
ssh-keygen -t ed25519 -N '' -C kmcbeauty-cd -f ~/.ssh/kmcbeauty_cd
cat ~/.ssh/kmcbeauty_cd.pub
```

서버의 배포 계정 `~/.ssh/authorized_keys` 에 한 줄 추가한다(`ssh-ed25519 AAAA...` 부분을 위 공개키로):

```text
restrict,command="/srv/server-mini2/web/kmcbeauty-fastapi/scripts/deploy_entry.sh" ssh-ed25519 AAAA... kmcbeauty-cd
```

- `restrict` 는 포워딩 · PTY · `~/.ssh/rc` 를 끄고, `command=` 때문에 클라이언트가 무엇을 보내도
  deploy_entry.sh 만 실행된다(보낸 문자열은 `$SSH_ORIGINAL_COMMAND` 로만 전달되어 형식 검사를 받는다).
- deploy_entry.sh 는 실행 비트(git 100755)가 있어야 강제 명령으로 실행된다(가드 테스트가 검사).
- 동작 확인(운영자 PC — 이 키로는 셸이 열리지 않는다):

  ```bash
  ssh -i ~/.ssh/kmcbeauty_cd -p <ssh-port> <deploy-user>@<ssh-host> whoami </dev/null; echo "exit=$?"
  # → "deploy_entry: 허용되지 않는 요청입니다 …" exit=2
  ssh -i ~/.ssh/kmcbeauty_cd -p <ssh-port> <deploy-user>@<ssh-host> \
    'deploy 0000000000000000000000000000000000000000' </dev/null; echo "exit=$?"
  # → git fetch 뒤 "origin/main 에서 찾을 수 없는 커밋" exit=2 (접속 · 강제 명령 · fetch 까지 정상)
  ```

### (f) GitHub 저장소 시크릿

```bash
# 운영자 PC, 이 저장소 루트에서(gh auth login 완료).
# 1) 호스트 키 — DEPLOY_SSH_HOST 와 똑같은 이름으로 받는다. 호스트 이름이 들어 있으니 저장소 밖에 둔다.
ssh-keyscan -p <ssh-port> <ssh-host> > ~/.ssh/kmcbeauty_known_hosts
ssh-keygen -lf ~/.ssh/kmcbeauty_known_hosts
# 2) 서버에서(이미 신뢰하는 세션) 지문을 보고 1) 과 같은지 대조한다 — 다르면 중단
for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done
# 3) 등록(형식 검증 후 지문을 한 번 더 보여 준다) · 점검
DEPLOY_SSH_HOST=<ssh-host> DEPLOY_SSH_PORT=<ssh-port> DEPLOY_SSH_USER=<deploy-user> \
DEPLOY_SSH_KEY_FILE=~/.ssh/kmcbeauty_cd DEPLOY_SSH_KNOWN_HOSTS_FILE=~/.ssh/kmcbeauty_known_hosts \
  ./scripts/setup_github_secrets.sh
./scripts/setup_github_secrets.sh --check
```

- `DEPLOY_SSH_HOST` 는 GitHub 호스티드 러너(인터넷)에서 `<ssh-port>` 로 닿아야 한다(공유기 포트 포워딩 ·
  방화벽). Cloudflare 프록시(주황 구름) 이름은 SSH 가 통과하지 못하므로 DNS-only 이름이나 공인 IP 를 쓴다.
- 네트워크로 받은 키는 중간자 공격을 걸러내지 못한다. 지문 대조(2)를 건너뛰지 않는다.
- GitHub `production` environment 에 필수 리뷰어를 두지 않는다(있으면 승인 대기로 멈춘다 — 지운다).
  environment 가 없으면 첫 실행 때 자동으로 생긴다.
- 예전 `SWARM_SSH_*` 시크릿이 남아 있으면 지운다(6절).

### (g) nginx (server-mini2 저장소)

1. (사용자) Cloudflare 에 `api-beauty.dongri.kr` 레코드를 추가한다(프록시 켬 — dongri.kr 과 같은 방식).
   인증서는 기존 `dongri.kr · *.dongri.kr` Cloudflare origin 인증서를 그대로 쓴다.
2. 원본 설정 **`deploy/nginx/api-beauty.conf`**(이 저장소)를 server-mini2 의 `nginx/conf.d/` 로 복사한다.
   내용을 여기에 다시 적지 않는다 — 고칠 때는 이 저장소의 원본과 서버 사본을 같이 고친다.

   ```bash
   cp /srv/server-mini2/web/kmcbeauty-fastapi/deploy/nginx/api-beauty.conf /srv/server-mini2/nginx/conf.d/api-beauty.conf
   docker exec nginx nginx -t && docker exec nginx nginx -s reload     # server-mini2 규칙에 따라 승인 후
   curl -sS -o /dev/null -w '%{http_code}\n' https://api-beauty.dongri.kr/health   # 배포 전 502, 배포 후 200
   ```

   - 업스트림은 `resolver 127.0.0.11` + 변수 proxy_pass(`kmcbeauty-api:8000`)라 요청 시점에 이름을 해석한다 —
     컨테이너가 없어도 nginx 가 뜨고, 재배포로 컨테이너 주소가 바뀌어도 리로드가 필요 없다.
   - Cloudflare 대역의 `CF-Connecting-IP` 로 실제 접속자 주소를 복원하고, 접근 기록에는 경로만 남긴다
     (검색어가 질의 문자열에 실리므로). 요청 횟수 제한은 앱이 한다.
   - server-mini2 의 `.gitignore` 는 화이트리스트 방식이라 새 파일이 추적되는지 `git status` 로 확인한다.

### (h) server-mini2 `docs/PORTS.md` 등록

| 표 | 추가할 줄 |
|---|---|
| 도메인 → 업스트림 | `api-beauty.dongri.kr` · `api-beauty.conf` · `kmcbeauty-api:8000` (resolver 재해석) · JWT(앱) |
| 컨테이너 내부 포트 · 고정 IP (`shared_network_prod`) | `kmcbeauty-api` · 동적 · 8000 · web/kmcbeauty-fastapi (`compose.prod.yaml`) / `kmcbeauty-celery-worker` · `kmcbeauty-celery-beat` · 동적 · — · 같은 프로젝트 |
| Redis DB | `5` kmcbeauty 캐시 · 리프레시 토큰 · Celery 브로커/결과 / `6` kmcbeauty rate limit |

### (i) 병합 → CD → 검증

1. (a)~(h) 를 확인하고 PR 을 `main` 에 병합한다 → CD: CI → build/push → 이미지 스모크 → deploy(SSH 강제
   명령) → `deploy.sh`. 첫 실행은 컨테이너 **신규 생성**이다(직전 이미지 없음).
2. Actions 로그에서 `✓ 배포 완료 ghcr.io/antam2010/kmcbeauty-api:<sha>` 를 확인한다. 실패하면 종료 코드로
   단계를 안다(6절 표). 전제 실패(2)는 서버를 바꾸지 않으므로 고쳐서 Actions 에서 재실행하면 된다.
   - 이미지 pull 실패(3): GitHub 패키지 `kmcbeauty-api` 설정에서 이 저장소와 연결됐는지, Manage Actions access
     에 이 저장소(Read)가 있는지 확인한다(GHCR 패키지는 기본 비공개). 저장소가 공개라 패키지를 공개로
     바꿔도 된다 — 그러면 토큰 없이 pull 한다.
3. 서버에서 확인:

   ```bash
   docker compose -p kmcbeauty ps                    # 세 컨테이너 Up, api (healthy)
   docker exec postgresql psql -U postgres -d kmcbeauty -tAc 'SELECT version_num FROM alembic_version'   # 5506c8cf9e2f
   docker exec postgresql ls -l /var/lib/postgresql/backup | grep kmcbeauty-   # 배포 전 백업
   docker logs --tail 30 kmcbeauty-celery-worker     # "Connected to redis://:**@redis:6379/5" · "ready."
   docker logs --tail 30 kmcbeauty-celery-beat
   curl -fsS https://api-beauty.dongri.kr/health
   ```

4. 클라이언트 IP 복원: 같은 IP 에서 로그인을 6번 연속 시도하면 6번째가 429 다. `docker logs kmcbeauty-api` 의
   접근 로그 IP 가 nginx 주소가 아니라 실제 IP 인지 본다(nginx real_ip · FORWARDED_ALLOW_IPS 정상).
5. Firebase 가 `{}` 이면 `Failed to initialize Firebase Admin SDK` 로그는 예상된 것이다.

### (j) 롤백

| 시점 | 조치 |
|---|---|
| 첫 배포 실패(병합 직후) | 되돌릴 직전 컨테이너가 없다. Actions 로그 · `docker logs` 로 원인을 고쳐 다시 병합하거나 재실행한다. 내려야 하면 `docker compose -p kmcbeauty down` — 네트워크 · `secrets/` · DB · nginx 는 그대로 남고 nginx 는 이 호스트만 502 가 된다 |
| 이후 배포 뒤 앱 문제 | `deploy.sh` 가 출력한 명령: `IMAGE_TAG=<직전 이미지> docker compose -p kmcbeauty -f compose.prod.yaml up -d --wait` — 스키마는 그대로(7절) |
| 특정 커밋으로 고정 | 서버에서 `git checkout --detach <sha> && scripts/deploy.sh <sha>`. 그 사이 새 마이그레이션이 있으면 4 로 멈춘다 → 위 이미지 롤백 사용 |
| 데이터 복구(최후 수단, 파괴적) | 컨테이너를 멈추고 배포 직전 백업으로 되돌린다(백업 이후에 쓰인 데이터는 사라진다) |

```bash
cd /srv/server-mini2/web/kmcbeauty-fastapi
docker compose -p kmcbeauty stop
docker exec postgresql pg_restore -U postgres -d kmcbeauty --clean --if-exists \
  /var/lib/postgresql/backup/kmcbeauty-<시각>.dump
docker compose -p kmcbeauty start
```

- 백업 파일은 배포마다 쌓인다. 보존 기간은 server-mini2 의 백업 운영에 맞춰 정리한다.
