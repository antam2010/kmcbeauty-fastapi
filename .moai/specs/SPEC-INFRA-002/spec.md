---
id: SPEC-INFRA-002
version: 1.1.0
status: completed
created: 2026-07-07
updated: 2026-07-07
author: antam2010
priority: HIGH
issue_number: null
---

# SPEC-INFRA-002: 시크릿 관리 하드닝 — Docker secrets · GitHub 시크릿 자동화 · CD 연동

## HISTORY

- 2026-07-07 (v1.1.0): 구현 완료. status approved → completed. 4개 커밋(b6038b3/87188e2/ba0f114/1509481)으로
  REQ-INFRA-101~115 전체 구현. Sprint Contract(thorough harness, strict profile) 기반 구현, evaluator-active 최종
  PASS 판정. Implementation Notes 섹션 추가.
- 2026-07-07 (v0.1.0): 최초 작성. SPEC-INFRA-001(배포 하드닝 · CI/CD)이 도입한 산출물 위에
  **증분**으로 시크릿 관리를 하드닝한다. 설정 로딩을 pydantic-settings BaseSettings +
  `secrets_dir` 로 전면 전환(사용자 결정), 민감 env 를 Docker secret 으로 이관, GitHub Actions
  시크릿 등록/회전 자동화, CD 파이프라인 시크릿 주입·검증 통합을 4개 마일스톤으로 정리.
  외부 시크릿 매니저(Vault/AWS)·GitLab·Kubernetes 는 명시적 범위 제외.
- 2026-07-07 (v0.1.0, annotation cycle 1): 미해결 설계 쟁점 4건 확정. (1) REDIS_URL 은
  `.env.prod` 잔류(시크릿화 제외), (2) 마이그레이션 시크릿 주입은 일회성 Swarm 서비스, (3) 필드
  타입은 plain `str` 유지(SecretStr 미채택), (4) Docker secret 은 소문자 external 이름 +
  `secrets.target` 매핑(target 불변 → 앱 코드 회전 무관). REQ-106/107/109/112/114 문구 정합.
  status: draft 유지.

---

## 배경 (Why)

SPEC-INFRA-001 은 무중단 배포·CI/CD 를 구축하고 **Firebase 서비스 계정 키 1건만** Docker
secret 으로 전환했다. 나머지 민감 값은 여전히 매니저 노드의 `.env.prod` 평문 파일에 집중되어
다음 취약점이 남는다.

- **평문 env 집중**: SECRET_KEY · FERNET_KEY · DATABASE_URL 등 모든 자격 증명이 단일
  `.env.prod` 평문에 존재. 파일 유출 시 전면 노출되며 Swarm secret(at-rest 암호화 · 노드 간
  TLS · tmpfs 마운트)의 이점을 받지 못한다.
- **os.getenv 산개**: `app/core/config.py:5-14`, `app/database.py:12`, `app/core/redis_client.py:9`,
  `celery_app.py:9`(저장소 루트), `app/core/firebase.py:12`, `alembic/env.py:14-19` 가 각기
  import 시점에 개별적으로 환경변수를 읽는다. 단일 진실원천이 없어 시크릿 소스 변경이 어렵다.
  특히 `REDIS_URL` 은 `redis_client.py` 와 `celery_app.py` 에 기본값이 **중복 정의**되어 있다.
- **회전 절차 부재**: 시크릿 회전 runbook 이 없다. Docker secret 은 불변이라 값만 바꿔서는
  회전되지 않는데(스택 참조 미변경 시 재배포 no-op) 이 사실이 문서화되어 있지 않다.
- **GitHub 시크릿 수기 관리**: `SWARM_SSH_HOST/USER/KEY` 를 UI 에서 수동 등록하며 인벤토리·
  회전 절차가 없다.

이 SPEC 은 위 공백을 메운다. 상세 코드 근거는 `research.md` 참조.

## 확정된 기술 결정 (재검토 대상 아님)

- **플랫폼: GitHub Actions**(GitLab 아님 — 사용자 오타 확인). SPEC-INFRA-001 의 GHCR·SSH 배포
  파이프라인을 그대로 계승.
- **시크릿 로딩: pydantic-settings BaseSettings + `secrets_dir="/run/secrets"` 전면 도입**
  (사용자 명시 결정). env fallback 유지, 테스트/로컬은 env-var 우선. `pydantic-settings==2.14.2`
  는 이미 `requirements.txt:86` 에 고정됨.
- **Docker secret 회전: 버전드 external 이름**(`kmc_..._v2`) + 스택 참조 교체 + 재배포.
  `secrets.target`(컨테이너 내 파일명)은 불변이라 **앱 코드는 회전에 무관**. SPEC-INFRA-001 의
  start-first 무중단 배포와 호환.
- **시크릿 필드 타입: plain `str` 유지**(pydantic `SecretStr` 미채택). 소비처 변경을 최소화하기
  위함이며, 로그 노출 통제는 코드 리뷰 및 evaluator 보안 점검에 위임한다.
- **앱 코드 변경 범위**: 설정 로딩 계층과 그 소비처로 한정. 도메인 로직 변경 없음.

## 환경 및 전제 (Environment & Assumptions)

- SPEC-INFRA-001 산출물(`docker-stack.yml`, `.env.prod`, `.github/workflows/cd.yml`,
  `scripts/deploy_migrate.sh`, `docs/deployment.md`)이 존재하며 이 SPEC 은 이를 수정·확장한다.
- Swarm 매니저 노드에서 `docker secret create` 가 가능하고, GitHub CLI(`gh`)로 저장소 시크릿을
  등록할 수 있는 권한이 있다.
- 기존 Docker secret 패턴(`firebase_service_account`, `external: true`)을 신규 시크릿에도 적용한다.
- MySQL 은 운영 환경에서 컨테이너 외부(관리형/호스트)에 존재하며(`DATABASE_URL` 이 이를 가리킴)
  이 SPEC 에서 컨테이너화하지 않는다.
- 로컬 개발은 `.env`(비암호화)를 계속 사용한다. 시크릿 하드닝은 배포 환경에 한정한다.

---

## 요구사항 (Requirements, EARS)

EARS 키워드(WHEN / WHILE / WHERE / IF / THEN / SHALL)는 원문 형식을 유지한다.
REQ-ID 는 SPEC-INFRA-001(001~018)과 충돌을 피하기 위해 `REQ-INFRA-1XX` 를 사용한다.

### M1 — 설정 계층 재설계 (BaseSettings 전환) (Priority: High)

> [주의] 이 마일스톤은 기존 코드 리팩터링이다. **특성화 우선(characterization-first)** 으로
> 현행 동작을 스냅샷한 뒤 변경한다.

**REQ-INFRA-101 (BaseSettings + secrets_dir 도입)**
The system **shall** load application configuration through a single central `Settings` object
implemented with pydantic-settings `BaseSettings`, sourcing values from `secrets_dir="/run/secrets"`
with environment variables and `.env` as fallback.
**WHEN** a value is present both as an environment variable and as a `secrets_dir` file, the system
**shall** give the environment variable precedence, so that local development and the test suite
(`tests/conftest.py`) keep working unchanged.

**REQ-INFRA-102 (소비처 중앙 settings 임포트 전환)**
The system **shall** refactor every module that currently reads `os.getenv()` at import time
(`app/core/config.py`, `app/database.py`, `app/core/redis_client.py`, `celery_app.py`,
`app/core/firebase.py`, and value consumers such as `app/core/security.py`,
`app/services/auth_service.py`, `app/main.py`, `app/exceptions.py`, `app/api/auth.py`,
`app/utils/redis/auth.py`) to import the central settings object instead of reading environment
variables directly.
**WHILE** import-time side effects exist (`create_engine`, `redis.Redis.from_url`, `Celery(...)`,
`firebase initialize_app`), the system **shall** ensure these continue to receive their values from
the central settings object without changing their runtime behavior.

**REQ-INFRA-103 (REDIS_URL 이중 정의 통일)**
The system **shall** unify the duplicated `REDIS_URL` default currently defined in both
`app/core/redis_client.py:9` and `celery_app.py:9` into the central settings object as a single
source of truth.

**REQ-INFRA-104 (Sentry 선택적 처리)**
**WHERE** the `SENTRY_DSN` secret file or environment variable is absent, the system **shall**
degrade to an empty-string default and initialize without error (Sentry disabled), preserving the
current optional behavior.

**REQ-INFRA-105 (특성화 테스트로 동작 보존)**
**WHEN** the configuration-loading layer is refactored, the system **shall** first capture the
current config-loading behavior with characterization tests, and those tests **shall** pass both
before and after the refactor.
The system **shall** redesign test fixtures so that they keep working under BaseSettings — either by
relying on env-var precedence over `secrets_dir`, or by providing a temporary `secrets_dir`.

### M2 — Docker secrets 전환 (Priority: High)

**REQ-INFRA-106 (신규 Docker secrets 정의)**
The system **shall** define new Docker secrets for the sensitive fields `SECRET_KEY`, `FERNET_KEY`,
and `DATABASE_URL` in `docker-stack.yml` (top-level `secrets:`, `external: true`), following the
existing `firebase_service_account` pattern.
The system **shall** keep `REDIS_URL` in `.env.prod` and **shall not** promote it to a Docker secret,
because it carries no credentials (internal overlay DNS `redis://redis:6379/0` only).
비고: 추후 Redis 인증(비밀번호)이 도입되면 `REDIS_URL` 의 시크릿화는 이 SPEC 이 아닌 별도 SPEC 에서
다룬다. `REDIS_URL` 의 이중 정의 통일(REQ-INFRA-103)은 시크릿 여부와 무관하게 그대로 수행한다.

**REQ-INFRA-107 (서비스 마운트 및 secrets_dir 정합)**
**WHEN** the `api`, `celery_worker`, and `celery_beat` services start in Swarm, the system **shall**
mount every new secret using a lowercase external secret name mapped via `secrets.target` to a
container file name that equals the pydantic field name (예: external `kmc_secret_key_v1` →
`target: SECRET_KEY` → `/run/secrets/SECRET_KEY`).
The system **shall** keep the `target` (컨테이너 내 파일명) immutable across rotations while the
version suffix changes only on the external name, so that application code stays rotation-agnostic
(앱은 항상 `/run/secrets/SECRET_KEY` 만 읽는다).
The system **shall** mount required secrets on `celery_beat` as well, because it transitively imports
the settings object at load time (SPEC-INFRA-001 의 전이 import 규칙 계승).

**REQ-INFRA-108 (.env.prod 민감 키 이관 표기)**
The system **shall** remove the migrated sensitive keys from `.env.prod.example` (or mark them as
"managed via Docker secret") so the template documents only non-secret configuration and the secret
inventory.

**REQ-INFRA-109 (마이그레이션 컨테이너 시크릿 주입)**
**WHEN** the pre-deploy migration step runs (`scripts/deploy_migrate.sh`), the system **shall**
provide `DATABASE_URL` to the migration container by running it as a **one-off Swarm service**
(`docker service create --restart-condition=none --secret database_url … alembic upgrade head`), so
that the `database_url` secret is mounted under `/run/secrets/` (`docker run` 은 Swarm secret 을
마운트하지 못하므로 배제한다).
The system **shall** inspect the one-off service exit code and **shall not** proceed to
`docker stack deploy` unless the migration task completed successfully.
**IF** the migration task fails or cannot obtain `DATABASE_URL`, **then** the system **shall** fail
fast with a clear error rather than running against a wrong or empty target.

### M3 — GitHub 시크릿 관리 자동화 (Priority: Medium)

**REQ-INFRA-110 (시크릿 인벤토리 문서화)**
The system **shall** document the complete inventory of required GitHub Actions repository secrets
(`SWARM_SSH_HOST`, `SWARM_SSH_USER`, `SWARM_SSH_KEY`, and any newly introduced secrets), including
purpose and owner.

**REQ-INFRA-111 (gh 기반 등록 스크립트/절차)**
The system **shall** provide a `gh secret set` based script or documented procedure for registering
and updating GitHub Actions secrets, replacing manual UI entry.

**REQ-INFRA-112 (회전 절차 runbook)**
The system **shall** document a rotation runbook covering (a) the SSH deploy key and (b) Docker
secrets via versioned external names.
**WHEN** a Docker secret is rotated, the runbook **shall** prescribe creating a versioned external
secret (`kmc_secret_key_v2`), swapping only the external-name reference in `docker-stack.yml` while
keeping `target` unchanged (`SECRET_KEY`), and redeploying, **so that** rotation stays compatible with
start-first zero-downtime rollout and application code remains untouched (앱은 항상 동일 target 파일을
읽으므로 회전에 무관하며, 롤링 중 신·구 레플리카가 각자 마운트된 시크릿을 사용한다).

### M4 — CD 파이프라인 연동 (Priority: Medium)

**REQ-INFRA-113 (배포 시 시크릿 존재 사전 검증)**
**WHEN** the CD pipeline deploys to the Swarm manager, the system **shall** verify that every
required Docker secret exists before running `docker stack deploy`.
**IF** a required secret is missing, **then** the pipeline **shall** abort with a clear error and
**shall not** proceed with the deployment.

**REQ-INFRA-114 (버전드 시크릿 교체 배포 플로우)**
**WHERE** a secret rotation is in progress, the CD/deploy flow **shall** support deploying with the
swapped versioned external secret name (target 불변) while preserving `order: start-first`, so that
the rollout remains zero-downtime and no application code change is required for the rotation.

**REQ-INFRA-115 (배포 문서 갱신)**
The system **shall** update `docs/deployment.md` to describe Docker-secret creation, the GitHub
secret inventory, the rotation runbook, and the pre-deploy secret verification step.

---

## Exclusions (What NOT to Build)

- **외부 시크릿 매니저**(HashiCorp Vault / AWS Secrets Manager / GCP Secret Manager 등): 도입하지
  않는다. Docker Swarm secret + GitHub Actions secret 으로 충분한 규모.
- **GitLab / GitLab CI**: 도입하지 않는다. 플랫폼은 GitHub 로 확정.
- **Kubernetes / K8s Secrets**: SPEC-INFRA-001 의 제외를 승계. Swarm 유지.
- **애플리케이션 도메인 로직 변경**: 코드 변경은 설정 로딩 계층과 그 소비처로 한정한다. 인증·
  암호화·DB·Celery 의 **런타임 동작**은 보존한다(특성화 테스트로 검증).
- **시크릿 암호화 KMS/HSM · 봉투 암호화(envelope encryption)**: 범위 밖.
- **투기적 기능**: 시크릿 자동 만료·자동 회전 스케줄러, 동적 시크릿 발급 등은 현 요구에 없으므로
  도입하지 않는다(회전은 문서화된 수동 runbook).

---

## 추적성 (Traceability)

| 요구사항 | 마일스톤 | 대상 아티팩트(예상) |
|---|---|---|
| REQ-INFRA-101, 102, 103 | M1 | `app/core/config.py`(BaseSettings 재작성), `app/database.py`, `app/core/redis_client.py`, `celery_app.py`, `app/core/security.py`, `app/services/auth_service.py`, `app/main.py`, `app/exceptions.py`, `app/api/auth.py`, `app/utils/redis/auth.py`, `alembic/env.py` |
| REQ-INFRA-104, 105 | M1 | `app/core/config.py`, `app/core/sentry.py`, `tests/conftest.py`, 신규 특성화 테스트 |
| REQ-INFRA-106, 107, 108 | M2 | `docker-stack.yml`, `.env.prod.example` |
| REQ-INFRA-109 | M2 | `scripts/deploy_migrate.sh`, `alembic/env.py` |
| REQ-INFRA-110, 111, 112 | M3 | 시크릿 등록/회전 스크립트, `docs/deployment.md` |
| REQ-INFRA-113, 114, 115 | M4 | `.github/workflows/cd.yml`, `scripts/deploy_migrate.sh`, `docs/deployment.md` |

---

## 확정된 설계 결정 (Annotation Cycle 1, 2026-07-07)

주석 사이클 1 에서 아래 4개 쟁점이 모두 확정되었다. 관련 REQ 문구에 반영 완료.

1. **REDIS_URL 민감도** — 자격 증명 없는 내부 오버레이 DNS 이므로 **`.env.prod` 잔류**(시크릿화
   제외). 추후 Redis 인증 도입 시 시크릿화는 별도 SPEC 에서 다룬다. 이중 정의 통일(REQ-INFRA-103)은
   시크릿 여부와 무관하게 수행(REQ-INFRA-106).
2. **마이그레이션 컨테이너 시크릿 주입** — **일회성 Swarm 서비스**(`docker service create
   --restart-condition=none` + `--secret database_url`, 종료 코드 확인) 확정(REQ-INFRA-109).
3. **필드 타입 정책** — **plain `str` 유지**(SecretStr 미채택). 소비처 변경 최소화가 이유이며, 로그
   노출 통제는 코드 리뷰 및 evaluator 보안 점검으로 위임한다.
4. **Docker secret 이름 규약** — **소문자 external 이름 + `secrets.target` 매핑**으로 컨테이너 내
   파일명을 pydantic 필드명에 정렬(예: `kmc_secret_key_v1` → `target: SECRET_KEY`). 버전 suffix 는
   external 이름에만 붙고 target 은 불변이므로 **앱 코드가 회전에 무관**해진다(REQ-INFRA-107/112/114).

---

## Implementation Notes (2026-07-07)

### 구현 커밋 매핑

| 커밋 | 마일스톤 | 내용 요약 |
|------|---------|----------|
| `b6038b3` | M1 | BaseSettings 전환, 소비처 중앙 임포트 통일, REDIS_URL 이중 정의 통일, 특성화 테스트 |
| `87188e2` | M2 | Docker secrets 전환 (docker-stack.yml), .env.prod.example 민감 키 이관 표기, deploy_migrate.sh 일회성 Swarm 서비스 |
| `ba0f114` | M3+M4 | GitHub 시크릿 등록 스크립트(setup_github_secrets.sh), 회전 runbook, CD 파이프라인 시크릿 존재 검증, docs/deployment.md 갱신 |
| `1509481` | 기록 | SPEC 문서화 및 progress.md 갱신 |

### Sprint Contract 기반 구현 결과

- 하네스 레벨: **thorough**, evaluator 프로파일: **strict**
- 계약 위반 위험으로 사전 명시된 5건 전부 해소됨:
  1. `alembic/env.py` BaseSettings 인스턴스화 시 필수 필드 검증 실패 위험 → `DATABASE_URL` 외 `SECRET_KEY`/`FERNET_KEY` 도 마이그레이션 Swarm 서비스에 마운트하여 해소
  2. `celery_beat` 전이 임포트로 인한 시크릿 마운트 누락 위험 → `celery_beat` 서비스에 모든 필수 시크릿 마운트 추가
  3. `conftest.py` BaseSettings env-var 우선순위 미보장 위험 → env-var precedence 확인 및 FERNET_KEY 유효 44자 키로 교체
  4. `secrets.target` 불변 규약 미준수 위험 → target 필드 명시, external 이름에만 버전 suffix 적용
  5. CD 파이프라인 시크릿 존재 사전 검증 누락 위험 → `cd.yml`에 `docker secret inspect` 단계 추가

### evaluator-active 최종 판정

| 차원 | 점수 |
|------|------|
| Security | 100 |
| Functionality | 100 |
| Craft | 92 |
| Consistency | 95 |
| **종합 판정** | **PASS** |

### 구현 편차 (수용됨)

- **마이그레이션 Swarm 서비스 시크릿 마운트 확장**: 일회성 Swarm 서비스가 `DATABASE_URL` 외에도 `SECRET_KEY`/`FERNET_KEY` 를 마운트한다. alembic이 full `Settings` 인스턴스화 시 필수 필드 검증을 수행하므로 필요한 편차이다. 비시크릿 필드(`REDIS_URL`, `SENTRY_DSN` 등)는 `-e` 플래그로 전달.
- **M1 하위호환 별칭 최종 정리**: M1 구현 중 생성된 하위호환 별칭은 dead code 제거 단계(ba0f114)에서 정리 완료.

### 테스트 결과

- `app/core/config.py` 커버리지: **100%**
- 전체 테스트: **90 passed** (기존 fastapi-pagination 비호환 5건 제외, SPEC-INFRA-001 이전부터 존재)

### 라이브 유예 항목 (AC-5/7/9/10)

아래 인수 기준은 운영 환경에서만 검증 가능하여 `docs/deployment.md` 리허설 절차로 대체한다.

| AC | 내용 | 대체 검증 |
|----|------|---------|
| AC-5 | 실 마운트 검증 (`docker exec cat /run/secrets/SECRET_KEY`) | deployment.md §"시크릿 검증 절차" |
| AC-7 | 마이그레이션 E2E (Swarm 서비스 종료 코드 0 확인) | deployment.md §"마이그레이션 절차" |
| AC-9 | 실패 시 abort 동작 (잘못된 시크릿으로 실행) | deployment.md §"장애 대응 runbook" |
| AC-10 | 무중단 회전 (v1→v2 교체 후 헬스체크) | deployment.md §"시크릿 회전 runbook" |

### 운영 사전 작업 (미완료 — 운영팀 수행 필요)

```bash
# 1. Swarm 매니저 노드에서 시크릿 3종 생성
echo -n "<값>" | docker secret create kmc_secret_key_v1 -
echo -n "<값>" | docker secret create kmc_fernet_key_v1 -
echo -n "<값>" | docker secret create kmc_database_url_v1 -

# 2. 서버 .env.prod에서 3키 제거 (SECRET_KEY, FERNET_KEY, DATABASE_URL)

# 3. GitHub Actions 시크릿 등록
bash scripts/setup_github_secrets.sh
```
