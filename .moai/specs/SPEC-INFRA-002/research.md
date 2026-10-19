# SPEC-INFRA-002 리서치 (research.md)

> 시크릿 관리 하드닝. SPEC-INFRA-001(배포 하드닝 · CI/CD)이 도입한 산출물
> (`docker-stack.yml`, `.env.prod`, `.github/workflows/cd.yml`, `scripts/deploy_migrate.sh`)
> 위에 **증분(incremental)** 으로 쌓는다. 아래 findings 는 Explore 에이전트 결과를
> 코드 재확인(file:line)으로 검증한 것이다.

## 배경 및 목적 (Why)

SPEC-INFRA-001 은 Firebase 서비스 계정 키 하나만 Docker secret 으로 전환했고, 나머지
민감 값(SECRET_KEY, FERNET_KEY, DATABASE_URL 등)은 여전히 `.env.prod` 평문 파일에
의존한다. `.env.prod` 는 `.gitignore` 로 저장소에서 제외되지만(`.gitignore:6`), 다음의
구조적 취약점이 남아 있다.

- **평문 env 파일 집중**: 모든 자격 증명이 매니저 노드의 단일 `.env.prod` 평문에 모여
  있어 파일 유출 시 전면 노출된다. Swarm secret(암호화 at-rest, 노드 간 TLS 전송,
  메모리 tmpfs 마운트)의 이점을 받지 못한다.
- **os.getenv 산개(散開)**: 각 모듈이 import 시점에 개별적으로 `os.getenv()` 를 호출해
  값을 읽는다. 시크릿 소스를 secrets_dir 로 바꾸려면 소비처를 일일이 수정해야 한다.
- **회전 절차 부재**: 시크릿 회전(rotation) 방법이 문서화되어 있지 않다. Docker secret 은
  불변(immutable)이라 값만 바꿔도 서비스가 갱신되지 않는다.
- **GitHub Actions 시크릿 수기 관리**: `SWARM_SSH_*` 시크릿을 UI 에서 수동 등록하며,
  인벤토리·회전 절차가 없다.

이 SPEC 은 (a) 민감 env → Docker secret 전환, (b) GitHub Actions 시크릿 등록/회전 자동화,
(c) CD 파이프라인의 시크릿 주입·검증 통합을 다룬다.

## 확정된 사용자 결정 (재검토 대상 아님)

1. **플랫폼: GitHub** (GitLab 아님 — 사용자 오타 확인).
2. **범위(3가지 전부)**: (a) 민감 env → Docker secrets, (b) GitHub Actions 시크릿
   등록/관리 자동화 + 회전 절차, (c) CD 파이프라인 시크릿 주입 연동.
3. **시크릿 로딩 방식: pydantic-settings BaseSettings + `secrets_dir` 전면 도입**.
   `app/core/config.py` 를 BaseSettings 로 재작성(`secrets_dir="/run/secrets"` + env fallback),
   모든 소비처를 중앙 settings 객체 임포트로 리팩터링, 테스트 픽스처 재설계. 사용자는
   entrypoint/`*_FILE` 방식보다 blast radius 가 크다는 점을 인지하고도 이 방식을 명시적으로 선택.
4. **Docker secret 회전**: 버전드 external 이름(`kmc_..._v2`) + 스택 `source` 참조 교체 + 재배포.
   `secrets.target`(컨테이너 내 파일명)은 불변이라 앱 코드는 회전에 무관. SPEC-INFRA-001 의
   start-first 무중단 배포와 호환. (Cycle 1 에서 target 매핑 규약 확정)
5. **앱 코드 변경 범위**: 설정 로딩 계층과 그 소비처로 한정. 도메인 로직 변경 없음.

---

## 현재 상태 분석 (검증된 findings, file:line)

### 설정 로딩 실태

- `app/core/config.py:1-14` — `python-dotenv` 의 `load_dotenv()`(:5) 후 모듈 레벨에서
  `os.getenv()` 로 값 읽기. **pydantic-settings 는 현재 미사용**. 단, `pydantic-settings==2.14.2`
  는 `requirements.txt:86` 에 이미 고정되어 있어 `secrets_dir` 를 즉시 사용할 수 있다.
- 모든 민감 값은 **import 시점**에 읽힌다 → 시크릿 소스 변경은 import 경로 전체에 영향.

### 민감 필드 인벤토리 (7개)

| 필드 | 민감도 | 현재 로딩 위치(file:line) | Docker secret 대상 | 비고 |
|---|---|---|---|---|
| `SECRET_KEY` | 시크릿 | `config.py:8` | 예 | JWT 서명 키 |
| `ALGORITHM` | 비시크릿 | `config.py:9` | 아니오 | `HS256` 등, env 유지 |
| `FERNET_KEY` | 시크릿 | `config.py:14` | 예 | 대칭 암호 키(44자 base64) |
| `DATABASE_URL` | 시크릿 | `database.py:12` | 예 | 자격 증명 포함 |
| `REDIS_URL` | 비시크릿(확정) | `redis_client.py:9`, `celery_app.py:9` | 아니오 (`.env.prod` 잔류) | 내부 DNS·무자격, 이중 정의 통일 대상(REQ-103) |
| `SENTRY_DSN` | 준시크릿(선택) | `config.py:13` | 선택 | 빈 문자열 허용 필수 |
| `FIREBASE_SERVICE_ACCOUNT_KEY_PATH` | 시크릿(경로) | `firebase.py:12` | **이미 완료** | SPEC-INFRA-001 에서 Docker secret 화 |

비시크릿 설정값(회전 불필요): `ACCESS_TOKEN_EXPIRE_SECONDS`(`config.py:10`),
`REFRESH_TOKEN_EXPIRE_SECONDS`(`config.py:11`), `APP_ENV`(`config.py:12`).

### import 시점 로딩 지점 (전환 대상 전체)

| 모듈(file:line) | 읽는 값 | import 시점 부작용 |
|---|---|---|
| `app/core/config.py:5-14` | SECRET_KEY, ALGORITHM, ACCESS/REFRESH_TOKEN_EXPIRE_SECONDS, APP_ENV, SENTRY_DSN, FERNET_KEY | `int()` 캐스팅(:10-11) — None 이면 즉시 예외 |
| `app/database.py:12,16` | DATABASE_URL | `create_engine(DATABASE_URL)`(:16) 를 import 시점에 생성 |
| `app/core/redis_client.py:9,11` | REDIS_URL(기본값 `redis://redis:6379/0`) | `redis.Redis.from_url()`(:11) import 시점 생성 |
| `celery_app.py:9,13-14` (저장소 루트) | REDIS_URL(**기본값 중복 정의**) | `Celery(broker=…, backend=…)` import 시점 생성 |
| `app/core/sentry.py` | (DSN 은 `main.py:22` 에서 주입) | `init_sentry(dsn=…)` 호출 기반 |
| `app/core/firebase.py:12,22-27` | FIREBASE_SERVICE_ACCOUNT_KEY_PATH | import 시점 `initialize_app()`(:22-27) |
| `alembic/env.py:14-19,24` | (`.env` → `.env.{ENV}` 로드 후) DATABASE_URL | `from app.database import DATABASE_URL`(:24) |

### 중앙 settings 값의 소비처 (리팩터링 대상)

`from app.core.config import <NAME>` 형태로 값을 직접 임포트하는 지점 — settings 객체
임포트로 전환 필요.

- `app/core/security.py:8,22` — ALGORITHM, FERNET_KEY, SECRET_KEY. **주의**: `Fernet(FERNET_KEY.encode())`(:22)
  가 import 시점에 실행된다. Cycle 1 에서 필드 타입을 plain `str` 유지로 확정했으므로 `.encode()` 는
  `Fernet(settings.FERNET_KEY.encode())` 로 최소 변경만 하면 된다(`SecretStr` 를 채택했다면
  `.get_secret_value()` 정렬이 필요했을 지점).
- `app/services/auth_service.py:9-14` — ACCESS_TOKEN_EXPIRE_SECONDS, ALGORITHM, REFRESH_TOKEN_EXPIRE_SECONDS, SECRET_KEY.
- `app/main.py:22` — APP_ENV, SENTRY_DSN.
- `app/exceptions.py:7` — APP_ENV.
- `app/api/auth.py:6`, `app/utils/redis/auth.py:1` — REFRESH_TOKEN_EXPIRE_SECONDS.

### 기존 Docker secret 패턴 (SPEC-INFRA-001)

- `docker-stack.yml:187-192` — `secrets:` 최상위에 `firebase_service_account`(`external: true`).
- `docker-stack.yml:45-46,119-120,152-153` — `api`·`celery_worker`·`celery_beat` 세 서비스에
  마운트. `celery_beat` 는 `celery_app` 로드 시 `app.core.firebase` 를 **전이(transitive) import**
  하므로 시크릿이 필요(`docker-stack.yml:148-151` 주석). 신규 시크릿도 동일 전이 규칙 적용.
- 컨테이너 내부 경로는 `/run/secrets/<name>` 로 고정.

### 마이그레이션 경로의 분기 (핵심 포인트 — Cycle 1 에서 확정)

- `scripts/deploy_migrate.sh` 는 `docker run --rm --env-file .env.prod … alembic upgrade head`
  로 **일회성 컨테이너**를 실행한다. `docker run` 은 **Swarm secret 을 마운트할 수 없다**
  (`/run/secrets` 미제공). 따라서 마이그레이션 컨테이너가 DATABASE_URL 시크릿을 받는 방법을
  별도로 해결해야 한다.
- `alembic/env.py:14-19` 는 `.env` → `.env.{ENV}` 를 `load_dotenv` 로 읽고 `app.database` 에서
  DATABASE_URL 을 가져온다. BaseSettings 전환 후에는 alembic 도 동일 settings 를 참조하거나
  secrets_dir 를 인식해야 정합.

### 회전(rotation) 시맨틱

- Docker secret 은 **불변**이다. 같은 이름의 시크릿 값만 바꾸는 것은 불가능하며,
  시크릿 파일만 교체해도 서비스는 재배포되지 않는다(스택 파일의 `image`/`config` 참조가
  바뀌지 않으므로 `docker stack deploy` 가 no-op).
- 따라서 회전은 **버전드 external 이름**(`kmc_secret_key_v2`) 신규 생성 → `docker-stack.yml` 의
  `source` 를 `_v2` 로 교체(`target` 은 필드명으로 불변) → `docker stack deploy` 재배포 → 구 시크릿
  제거 순서. start-first 롤링과 호환되어 무중단으로 회전 가능(신·구 레플리카가 각자 마운트된 시크릿을
  사용하되 둘 다 `/run/secrets/<필드명>` 으로 보이므로 앱 코드는 회전에 무관).

### 테스트 호환성

- `tests/conftest.py:1-19` — `app.*` import 이전에 `os.environ.setdefault()` 로 테스트 값 주입
  (SECRET_KEY, ALGORITHM, ACCESS/REFRESH_TOKEN_EXPIRE_SECONDS, APP_ENV, SENTRY_DSN="",
  FERNET_KEY=44자 유효 키). BaseSettings 전환 시 **env-var 가 secrets_dir 보다 우선**해야
  기존 테스트가 그대로 통과한다. 대안: 임시 `secrets_dir`(tmp_path) 로 픽스처 재설계.
- 최근 커밋 `b6ff36c` 가 conftest 의 FERNET_KEY 를 유효한 44자 키로 교체 → 테스트가 실제
  Fernet 생성에 의존.

---

## 로딩 방식 후보 3안 및 선택 근거

| 방식 | 요지 | 장점 | 단점 | blast radius |
|---|---|---|---|---|
| **A. pydantic-settings + secrets_dir** (채택) | `config.py` 를 `BaseSettings` 로 재작성, `secrets_dir="/run/secrets"` + env fallback | 표준·타입 검증·단일 진실원천, 소비처가 `settings.X` 로 통일 | 소비처 전면 리팩터링, 테스트 픽스처 재설계, import-time 부작용 재검토 필요 | **큼** |
| B. entrypoint 에서 `*_FILE` → env 확장 | 컨테이너 진입 스크립트가 `SECRET_KEY_FILE` 을 읽어 `SECRET_KEY` env 로 export | 앱 코드 변경 최소, 12-factor 친화 | entrypoint 스크립트/Dockerfile 수정, 규약이 앱 밖에 숨음, alembic `docker run` 경로 별도 처리 | 중간 |
| C. 앱 내 파일 읽기 헬퍼 | `read_secret("secret_key")` 유틸을 각 소비처가 호출 | 점진 도입 가능 | 로딩 로직 산개 유지(현 문제 반복), 표준성 낮음 | 작지만 부채 |

**선택: A안 (사용자 명시 결정).** blast radius 가 가장 크지만, 설정 로딩을 단일 타입-검증
객체로 수렴시켜 향후 시크릿 추가/회전/검증을 한 곳에서 관리한다. 사용자는 이 트레이드오프를
인지하고 선택했다. 리스크는 **특성화 테스트(characterization-first)** 로 완화한다(M1).

---

## 리스크

| 리스크 | 영향 | 완화 |
|---|---|---|
| import-time 로딩 실패(시크릿 파일 부재/오타) | 앱·워커·beat·alembic 전면 기동 불가 | secrets_dir + env fallback + 명확한 검증 에러 메시지, 로컬은 `.env` 유지 |
| 소비처 전면 리팩터링 중 회귀 | 인증·암호화·DB·Celery 동작 파손 | 특성화 테스트로 현행 동작 스냅샷 후 리팩터링(REQ-INFRA-105) |
| 테스트 env-var 우선순위 붕괴 | 전체 테스트 스위트 레드 | env-var > secrets_dir 우선순위 보장, 또는 tmp secrets_dir 픽스처 |
| 마이그레이션 컨테이너가 시크릿 못 받음 | 배포 전 마이그레이션 단계 실패 → 배포 중단 | 일회성 Swarm 서비스 확정(`docker service create --secret …`), 종료 코드 확인 + fail-fast(REQ-INFRA-109) |
| 필드 타입 str 유지 시 로그 노출 | 시크릿이 로그/트레이스에 평문 노출 | plain `str` 확정(SecretStr 미채택), 로그 노출은 코드 리뷰·evaluator 보안 점검으로 통제 |
| Docker secret 회전 시맨틱 오해 | 값만 바꿔 회전 시도 → 반영 안 됨(조용한 실패) | 버전드 external 이름 교체(`source` 만, `target` 불변) runbook 강제(REQ-INFRA-112) |
| secrets_dir 파일명 불일치 | 시크릿을 못 읽고 fallback→기동 실패 또는 잘못된 값 | `secrets.target` = 필드명 규약 확정(REQ-INFRA-107) |
| REDIS_URL 을 시크릿으로 오분류 | 불필요한 시크릿 관리 부담 | `.env.prod` 잔류 확정(무자격 내부 DNS). 추후 Redis 인증 도입 시 별도 SPEC |

## 확정된 설계 결정 (Annotation Cycle 1, 2026-07-07)

주석 사이클 1 에서 아래 4개 쟁점이 모두 확정되었다. 관련 REQ/plan 문구에 반영 완료.

1. **REDIS_URL 민감도** — 자격 증명 없는 내부 오버레이 DNS(`redis://redis:6379/0`)이므로
   **`.env.prod` 잔류**(Docker 시크릿화 제외). 이중 정의 통일(REQ-INFRA-103)은 시크릿 여부와 무관하게
   수행. 추후 Redis 인증(비밀번호) 도입 시 시크릿화는 이 SPEC 이 아닌 별도 SPEC 에서 다룬다.
2. **마이그레이션 컨테이너 시크릿 주입 방식** — **일회성 Swarm 서비스**(`docker service create
   --restart-condition=none --secret source=kmc_database_url_v1,target=DATABASE_URL … alembic upgrade
   head`) 확정. 종료 코드 확인 로직 포함. 매니저 노드 파일 bind mount(노드 파일 관리 부담), 파일→`--env`
   주입(프로세스 env/히스토리 노출)은 배제.
3. **필드 타입 정책** — **plain `str` 유지**(`SecretStr` 미채택). 소비처 변경 최소화가 이유이며,
   `security.py:22` 의 `Fernet(FERNET_KEY.encode())` 도 최소 변경으로 유지. 로그 노출 통제는 코드
   리뷰 및 evaluator 보안 점검으로 위임.
4. **Docker secret 이름 규약** — **소문자 external 이름 + `secrets.target` 매핑**. external 이름에만
   버전 suffix(`kmc_secret_key_v1`)를 붙이고 `target` 은 필드명(`SECRET_KEY`)으로 불변 → 컨테이너 내
   파일은 항상 `/run/secrets/SECRET_KEY`. 결과적으로 **앱 코드/필드명이 회전에 무관**해진다.
