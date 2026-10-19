# SPEC-INFRA-002 태스크 분해 (tasks.md)

> Phase 1.5 산출물. plan.md 의 마일스톤 순서(M1 → M2 → M3·M4)를 준수한다.
> 각 태스크는 하나의 TDD/특성화 사이클로 완결 가능한 원자 단위다. 확정된 설계 결정 4건
> (spec.md "확정된 설계 결정")은 재검토하지 않는다.

## Task Decomposition
SPEC: SPEC-INFRA-002

| Task ID | Description | Requirement | Dependencies | Planned Files | Status |
|---|---|---|---|---|---|
| T-101 | (M1) 현행 config 동작·import 부작용 특성화 테스트 선작성. config.py 노출값(SECRET_KEY/ALGORITHM/토큰만료/APP_ENV/SENTRY_DSN/FERNET_KEY)과 import 시점 부작용(engine, redis_client, celery_app, firebase init, Fernet 생성)을 스냅샷. 리팩터링 전/후 모두 통과해야 함. | REQ-INFRA-105 | — | `tests/test_config_characterization.py` (신규) | done |
| T-102 | (M1) config.py 를 `class Settings(BaseSettings)` + `secrets_dir="/run/secrets"` + `env_file=".env"` 로 재작성, `settings = Settings()` 싱글턴 노출, env-var > secrets_dir 우선순위 보장, `SENTRY_DSN: str = ""` degrade. 소비처 무파손을 위해 모듈 레벨 하위호환 별칭(`SECRET_KEY = settings.SECRET_KEY` 등) 임시 유지. | REQ-INFRA-101, REQ-INFRA-104 | T-101 | `app/core/config.py`, `tests/test_config_settings.py` (신규: AC-2 우선순위, AC-4 Sentry degrade) | done |
| T-103 | (M1) 소비처 전면을 중앙 `settings` 임포트로 전환하고 import-time 부작용은 값 소스만 교체(동작 보존). REDIS_URL 이중 정의(redis_client.py:9·celery_app.py:9) 제거 → `settings.REDIS_URL` 단일 참조(REQ-103). 하위호환 별칭은 특성화 항목 1~2(config 모듈 레벨 노출값) GREEN 보존을 위해 config.py 에 유지(소비처는 대문자 직접 import 0건). | REQ-INFRA-102, REQ-INFRA-103 | T-102 | `app/core/security.py`, `app/services/auth_service.py`, `app/main.py`, `app/exceptions.py`, `app/api/auth.py`, `app/utils/redis/auth.py`, `app/database.py`, `app/core/redis_client.py`, `celery_app.py`, `alembic/env.py`, `app/core/config.py` | done |
| T-104 | (M2) docker-stack.yml 최상위 `secrets:` 에 소문자 external 이름 `kmc_secret_key_v1`·`kmc_fernet_key_v1`·`kmc_database_url_v1`(`external: true`) 정의. api·celery_worker·celery_beat 세 서비스에 `source`→`target=필드명`(SECRET_KEY/FERNET_KEY/DATABASE_URL) 매핑으로 마운트. | REQ-INFRA-106, REQ-INFRA-107 | T-103 | `docker-stack.yml` | done |
| T-105 | (M2) `.env.prod.example` 에서 SECRET_KEY/FERNET_KEY/DATABASE_URL 제거·"Docker secret 관리" 주석 대체, 비시크릿(ALGORITHM·토큰만료·APP_ENV·REDIS_URL)+시크릿 인벤토리만 유지. 저장소 평문 0건 검증. | REQ-INFRA-108 | T-104 | `.env.prod.example` | done |
| T-106 | (M2) `deploy_migrate.sh` 컨테이너 경로를 일회성 Swarm 서비스(`docker service create --restart-condition=none --secret source=kmc_database_url_v1,target=DATABASE_URL … alembic upgrade head`)로 개편. 종료 코드 폴링·확인, 실패 시 fail-fast, 성공 후 `docker service rm`. `docker run --env-file` 제거. | REQ-INFRA-109 | T-104 | `scripts/deploy_migrate.sh`, `alembic/env.py` | done |
| T-107 | (M3, M2 후반과 병렬 가능) GitHub Actions 저장소 시크릿 인벤토리(SWARM_SSH_HOST/USER/KEY·용도·소유자·회전주기) 문서화 + `gh secret set` 기반 등록 스크립트/절차 제공(UI 수동 등록 대체). | REQ-INFRA-110, REQ-INFRA-111 | T-104 | `docs/deployment.md`, `scripts/setup_github_secrets.sh` (신규) | done |
| T-108 | (M3, T-107 과 순차: 동일 파일) 회전 runbook 문서화 — (a) SSH 배포 키 회전, (b) 버전드 Docker secret 회전(`source` 만 `_v2` 교체, `target` 불변, 재배포, 구 시크릿 제거). 서명키(SECRET_KEY) grace 기간 신·구 병행 검증 주의 표기. | REQ-INFRA-112 | T-104, T-107 | `docs/deployment.md` | done |
| T-109 | (M4) CD 시크릿 사전 검증 게이트: 배포 SSH 스크립트 선두(마이그레이션 이전)에서 `docker secret ls` 로 필수 시크릿 존재 확인, 누락 시 `exit 1` abort. 버전드 교체 배포가 `order: start-first` 를 보존하도록 보장(앱 코드 무변경). | REQ-INFRA-113, REQ-INFRA-114 | T-104, T-106 | `.github/workflows/cd.yml`, `scripts/deploy_migrate.sh` | done |
| T-110 | (M4, 최종 통합) `docs/deployment.md` 통합 갱신: Docker secret 생성 절차·인벤토리·회전 runbook·사전 검증 단계를 정합. SPEC-INFRA-001 배포 리허설 절과 일치. | REQ-INFRA-115 | T-107, T-108, T-109 | `docs/deployment.md` | done |

## 검증 방법 (로컬 검증 가능 vs 라이브 유예)

| Task ID | 검증 방법 | 유형 |
|---|---|---|
| T-101 | `pytest tests/test_config_characterization.py` (리팩터링 전 GREEN 확인) | 로컬 |
| T-102 | `pytest` (특성화 GREEN 유지 + 신규 AC-2 tmp secrets_dir 우선순위·AC-4 Sentry degrade 단위 테스트) | 로컬 |
| T-103 | `pytest` (특성화 GREEN = 동작 보존) + `git grep -n 'os.getenv("REDIS_URL"'` = 0건(AC-3) + 이관 모듈에 잔존 `os.getenv(` 부재 grep | 로컬 |
| T-104 | `docker stack config -c docker-stack.yml`(또는 `docker compose config`) 정적 검증 + 각 마운트 `target` == 필드명 grep(AC-5 정적). 실제 `/run/secrets/<필드명>` 마운트는 **라이브 유예**(AC-5 라이브) | 로컬 + 라이브 유예 |
| T-105 | `git grep` 민감 값 패턴 0건(AC-6) + `.gitignore` 에 `.env.prod` 유지 확인 + `.env.prod.example` 내용 검토 | 로컬 |
| T-106 | `deploy_migrate.sh` 정적 검사: `docker service create --secret` + 종료코드 확인 로직 존재, `docker run --env-file` 부재(AC-7 정적) + `bash -n`/shellcheck. 실제 마이그레이션 E2E 는 **라이브 유예**(AC-7 라이브) | 로컬 + 라이브 유예 |
| T-107 | `docs/deployment.md` 인벤토리 표 검토 + `setup_github_secrets.sh` 존재·`bash -n`/shellcheck(AC-8). 실제 `gh secret set` 등록은 repo admin 필요 **라이브 유예** | 로컬 + 라이브 유예 |
| T-108 | `docs/deployment.md` runbook 단계·명령 완결성 검토(AC-8) | 로컬 |
| T-109 | `cd.yml` YAML lint(actionlint 스타일) + 사전 검증 단계(`docker secret ls`)·abort 로직 정적 검사(AC-9 정적). 누락 시 abort 동작·무중단 회전(AC-10)은 **라이브 유예** | 로컬 + 라이브 유예 |
| T-110 | `docs/deployment.md` 통합 문서 검토(4개 절 정합) | 로컬 |

## 크리티컬 패스 및 병렬성

- **직렬 척추(gating spine)**: T-101 → T-102 → T-103 → T-104 → T-106 → T-109 → T-110.
- **M1 하드 직렬**: T-101(특성화 게이트) → T-102 → T-103. 특성화 테스트 없이 리팩터링 금지(REQ-105, characterization-first).
- **T-104 가 M2/M3/M4 전체를 게이팅**: 신규 시크릿 이름(`kmc_*_v1`)·target 규약이 여기서 확정되어야 T-105~T-110 이 참조 가능.
- **병렬 가능**: T-105(.env.prod.example, leaf)·T-107·T-108 은 T-104 이후 T-106 과 병렬 진행 가능. 단 **docs/deployment.md 를 공유하는 T-107·T-108·T-110 은 파일 쓰기 충돌 방지를 위해 순차**(T-107 → T-108 → T-110).
- **T-109 는 deploy_migrate.sh 를 T-106 과 공유** → T-106 완료 후 진행(직렬).
- **최종 통합 T-110**: T-107·T-108·T-109 완료 후 문서 정합.

## 라이브 유예 항목 요약 (배포 리허설 필요)

AC-5(실마운트)·AC-7(마이그레이션 E2E)·AC-9(사전검증 abort 동작)·AC-10(버전드 무중단 회전)은 Swarm 클러스터/GitHub Actions 환경이 필요하여 로컬 사이클에서 정적 검증까지만 수행하고 실동작은 배포 리허설로 유예한다(acceptance.md 기준).
