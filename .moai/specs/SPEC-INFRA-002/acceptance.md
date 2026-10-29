# SPEC-INFRA-002 인수 기준 (acceptance.md)

> 모든 기준은 객관적으로 검증 가능해야 한다(명령 출력, 파일 존재, 종료 코드, 테스트 결과).
> 각 AC 는 **로컬 검증 가능**(코드/테스트/정적 검사)과 **라이브 유예**(Swarm 클러스터 /
> GitHub Actions 필요)를 구분한다. SPEC-INFRA-001 acceptance.md 스타일을 계승한다.

## Given-When-Then 시나리오

### AC-1 — 중앙 settings 로 동작 보존 (REQ-INFRA-101, 102, 105 / 핵심) · 로컬 검증 가능

- **Given** `app/core/config.py` 가 pydantic-settings `BaseSettings` 로 재작성되고,
  리팩터링 전에 작성한 특성화 테스트가 존재한다.
- **When** 전체 테스트 스위트(`pytest`)를 실행한다.
- **Then** 특성화 테스트가 통과하고, 리팩터링 전 기준으로 통과하던 테스트가 모두 그대로 통과해야
  한다(신규 회귀 0건). `tests/conftest.py` 의 env-var 주입이 유효하려면 env-var 가 secrets_dir
  보다 우선해야 한다.

### AC-2 — secrets_dir 우선순위 및 env fallback (REQ-INFRA-101) · 로컬 검증 가능

- **Given** 임시 `secrets_dir` 에 `SECRET_KEY` 파일이 존재하고, 동시에 `SECRET_KEY` 환경변수가
  다른 값으로 설정되어 있다.
- **When** `Settings` 객체를 생성한다.
- **Then** 환경변수 값이 채택되어야 한다(env-var > secrets_dir). 반대로 환경변수가 없고
  secrets_dir 파일만 있으면 파일 값이 채택되어야 한다.

### AC-3 — REDIS_URL 단일 진실원천 (REQ-INFRA-103) · 로컬 검증 가능

- **Given** 설정 계층이 리팩터링되었다.
- **When** `app/core/redis_client.py` 와 `celery_app.py`(저장소 루트)에서 REDIS_URL 정의를 검사한다.
- **Then** 두 파일 모두 `settings.REDIS_URL` 하나만 참조하고, `os.getenv("REDIS_URL", ...)` 기본값
  중복 정의가 남아 있지 않아야 한다(grep 0건). `REDIS_URL` 은 Docker secret 이 아니라 `.env.prod`
  잔류 값이므로 신규 Docker secret 목록에 포함되지 않아야 한다(확정).

### AC-4 — Sentry 선택적 degrade (REQ-INFRA-104) · 로컬 검증 가능

- **Given** `SENTRY_DSN` secrets_dir 파일과 환경변수가 모두 부재하다.
- **When** 앱이 설정을 로드하고 Sentry 초기화 경로를 통과한다.
- **Then** `SENTRY_DSN` 이 빈 문자열로 degrade 되고, Sentry 는 비활성(미초기화)으로 오류 없이
  기동해야 한다.

### AC-5 — 신규 Docker secret 정의·마운트 (REQ-INFRA-106, 107) · 정적 검증 로컬 / 실마운트 라이브 유예

- **Given** `docker-stack.yml` 에 소문자 external 이름 시크릿(`kmc_secret_key_v1`/`kmc_fernet_key_v1`/
  `kmc_database_url_v1`)이 정의되고 `api`·`celery_worker`·`celery_beat` 에 `source`→`target` 매핑으로
  마운트된다(예: `source: kmc_secret_key_v1`, `target: SECRET_KEY`).
- **When** (정적) `docker-stack.yml` 을 검사하고, (라이브) 스택을 배포해 컨테이너 내부
  `/run/secrets/` 를 확인한다.
- **Then** (정적) 최상위 `secrets:` 정의와 3개 서비스의 마운트가 존재하고, 각 마운트의 `target` 이
  pydantic 필드명(`SECRET_KEY`/`FERNET_KEY`/`DATABASE_URL`)과 정확히 일치해야 한다. (라이브) 각
  시크릿 파일이 `/run/secrets/<필드명>` 에 읽기 가능하게 존재하고 앱이 해당 값으로 정상 기동해야 한다.

### AC-6 — 저장소 내 민감 값 평문 부재 (REQ-INFRA-108 / 보안 핵심) · 로컬 검증 가능

- **Given** 민감 키가 Docker secret 으로 이관되고 `.env.prod.example` 에서 제거·표기되었다.
- **When** 저장소 전체를 `git grep` 으로 검사한다(실제 SECRET_KEY/FERNET_KEY/DATABASE_URL 값 패턴,
  `.env.prod` 가 `.gitignore` 에 유지되는지 포함).
- **Then** 저장소 추적 파일에 민감 값 평문이 **0건**이어야 한다. `.env.prod.example` 은 시크릿을
  포함하지 않고 인벤토리/주석만 담아야 한다.

### AC-7 — 마이그레이션 컨테이너 시크릿 주입 (REQ-INFRA-109) · 정적/문서 로컬 / E2E 라이브 유예

- **Given** `scripts/deploy_migrate.sh` 가 **일회성 Swarm 서비스**(`docker service create
  --restart-condition=none --secret source=kmc_database_url_v1,target=DATABASE_URL … alembic upgrade
  head`)로 DATABASE_URL 을 마이그레이션 컨테이너에 주입한다.
- **When** (정적) `deploy_migrate.sh` 가 `docker run --env-file` 이 아닌 일회성 Swarm 서비스를
  사용하는지 검사하고, (라이브) 배포 전 마이그레이션 단계를 실행한다.
- **Then** (정적) `docker service create --secret …` + 종료 코드 확인 로직이 존재하고 `docker run
  --env-file .env.prod` 방식이 남아 있지 않아야 한다. (라이브) 마이그레이션 태스크가
  `/run/secrets/DATABASE_URL` 로 `alembic upgrade head` 를 수행하고 종료 코드 0 을 확인한 뒤에만
  `docker stack deploy` 로 진행하며, **IF** DATABASE_URL 을 얻지 못하거나 태스크가 실패하면
  fail-fast(비어있거나 잘못된 대상으로 실행 금지)해야 한다.

### AC-8 — GitHub 시크릿 인벤토리·등록·회전 문서화 (REQ-INFRA-110, 111, 112) · 로컬 검증 가능

- **Given** 시크릿 관리 문서와 등록 스크립트/절차가 존재한다.
- **When** `docs/deployment.md` 와 등록 스크립트를 검토한다.
- **Then** 필요한 GitHub Actions 시크릿 인벤토리(용도·소유자), `gh secret set` 기반 등록 절차,
  SSH 키·Docker secret(버전드) 회전 runbook 이 문서화되어 있어야 한다.

### AC-9 — CD 시크릿 사전 검증 게이트 (REQ-INFRA-113) · 정적 검증 로컬 / 동작 라이브 유예

- **Given** `.github/workflows/cd.yml`(또는 `deploy_migrate.sh` 앞단)에 시크릿 존재 검증 단계가 있다.
- **When** (라이브) 필요한 Docker secret 이 하나라도 없는 상태로 배포를 시도한다.
- **Then** 파이프라인이 `docker stack deploy` 이전에 명확한 오류로 **중단**되고, 배포를 진행하지
  않아야 한다.

### AC-10 — 버전드 시크릿 무중단 회전 (REQ-INFRA-112, 114) · 라이브 유예

- **Given** 스택이 `kmc_secret_key_v1`(target `SECRET_KEY`)로 정상 기동 중이고 외부에서 `/health` 를
  1초 간격으로 폴링 중이다.
- **When** runbook 절차대로 `kmc_secret_key_v2` 를 생성하고 `docker-stack.yml` 의 `source` 만
  `kmc_secret_key_v2` 로 교체(`target: SECRET_KEY` 불변)한 뒤 `docker stack deploy` 로 재배포한다.
- **Then** 폴링된 `/health` 실패가 **0건**(start-first 무중단)이고, 롤아웃 완료 후 모든 레플리카가
  `_v2` 시크릿을 `/run/secrets/SECRET_KEY` 로 마운트하며, **앱 코드/필드명 변경 없이** 구 시크릿
  (`kmc_secret_key_v1`)을 제거해도 정상 동작해야 한다.

---

## 엣지 케이스

- **secrets_dir 파일명 정합**: pydantic-settings 는 secrets_dir 파일명이 필드명과 일치해야 읽는다.
  external 이름은 소문자·버전드지만 `secrets.target` 을 필드명(`SECRET_KEY`)으로 고정하므로 컨테이너
  내 파일명은 항상 필드명과 일치한다. `target` 을 필드명과 다르게 두면 시크릿을 못 읽고 fallback→기동
  실패하거나 잘못된 값을 쓴다 → `target`=필드명 규약 + 배포 리허설에서 실마운트 검증.
- **import-time 실패**: 필수 시크릿 부재 시 `create_engine`/`Fernet`/`Celery`/firebase 초기화가
  import 시점에 실패한다 → 명확한 검증 에러 메시지로 원인을 노출(스택 트레이스만 남기지 않기).
- **시크릿 없는 로컬 dev**: 로컬은 `secrets_dir`(`/run/secrets`)가 없다 → env/`.env` fallback 으로
  정상 기동해야 하며, secrets_dir 부재가 예외를 던지지 않아야 한다.
- **celery_beat 전이 import**: `celery_app` 로드 시 `app.core.config`/`firebase` 를 전이 import
  하므로 beat 에도 신규 시크릿을 마운트하지 않으면 기동 실패(SPEC-INFRA-001 과 동일 함정).
- **회전 중 서명 키 불연속**: JWT 서명 키(SECRET_KEY)를 즉시 교체하면 진행 중 발급 토큰이 검증
  실패(간헐 401)할 수 있다 → 서명 키류는 grace 기간 신·구 병행 검증 전략을 runbook 에 명시.
- **회전 값만 교체(no-op 함정)**: 같은 external 이름 시크릿의 값만 바꾸면 재배포가 no-op 이라
  반영되지 않음 → 버전드 external 이름 교체(`source` 만 `_v2` 로, `target` 불변)를 강제(runbook).
- **마이그레이션 `docker run` 시크릿 미마운트**: `docker run` 은 Swarm secret 을 마운트하지 못함
  → REQ-109 확정안(일회성 Swarm 서비스)으로 개편하지 않고 `docker run --env-file` 을 유지하면
  마이그레이션이 시크릿을 못 받아 실패. AC-7 정적 검사로 `docker run --env-file` 잔존을 차단.

## 품질 게이트 기준 (TRUST 5)

- **Tested**: M1 특성화 테스트가 리팩터링 전/후 통과. secrets_dir 우선순위(AC-2)·Sentry
  degrade(AC-4) 단위 테스트 추가. 라이브 AC(5·7·9·10)는 배포 리허설 절차로 수행.
- **Readable**: `config.py` BaseSettings 필드에 목적 주석. `docker-stack.yml` 시크릿 정의·마운트에
  근거 주석. 회전 runbook 은 단계별 명령 포함.
- **Unified**: 모든 시크릿 소비가 단일 `settings` 객체를 경유. REDIS_URL 이중 정의 제거.
- **Secured**: 민감 값은 Docker secret / GitHub secret 으로만 주입. 저장소 평문 0건(AC-6).
  시크릿이 로그에 노출되지 않음.
- **Trackable**: 모든 변경은 SPEC-INFRA-002 및 REQ-ID 를 커밋/PR 에 참조.

## Definition of Done

- [ ] REQ-INFRA-101~105 반영: `config.py` BaseSettings 전환, 소비처 중앙 settings 임포트,
      REDIS_URL 통일, Sentry degrade, 특성화 테스트 통과(AC-1~4).
- [ ] REQ-INFRA-106~108 반영: 신규 Docker secret 정의·마운트(3개 서비스), `.env.prod.example`
      민감 키 이관(AC-5).
- [ ] REQ-INFRA-109 반영: 마이그레이션 컨테이너 시크릿 주입 방식 확정·구현, fail-fast(AC-7).
- [ ] REQ-INFRA-110~112 반영: GitHub 시크릿 인벤토리·`gh` 등록 절차·회전 runbook 문서화(AC-8).
- [ ] REQ-INFRA-113~115 반영: CD 시크릿 사전 검증(AC-9), 버전드 무중단 회전(AC-10),
      `docs/deployment.md` 갱신.
- [ ] Exclusions 준수: 외부 시크릿 매니저/GitLab/K8s 미도입, 도메인 로직 변경 없음.
- [ ] **저장소 내 민감 값 평문 0건**(`git grep` 검증, AC-6) · `.env.prod` 가 `.gitignore` 에 유지.
- [ ] SPEC-INFRA-001 무중단 배포 특성 보존: 회전·배포가 start-first 롤링을 깨지 않음.
