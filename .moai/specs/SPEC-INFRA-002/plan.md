# SPEC-INFRA-002 구현 계획 (plan.md)

> 시크릿 관리 하드닝. SPEC-INFRA-001 산출물 위에 증분으로 쌓는다. 시간 추정 대신 우선순위
> 기반 마일스톤 순서로 기술한다.

## 마일스톤 순서 및 의존성

```
M1 (설정 계층 재설계, High)
  └─ 모든 후속 작업의 전제. secrets_dir 를 읽는 중앙 settings 가 없으면
     Docker secret 을 앱이 소비할 수 없다. characterization-first 로 진행.
M2 (Docker secrets 전환, High)  ← M1 완료 후
  └─ 중앙 settings 가 secrets_dir 를 인식해야 서비스 마운트가 의미를 가진다.
M3 (GitHub 시크릿 자동화, Medium)  ← M2 와 병렬 가능(문서·스크립트 중심)
  └─ Docker secret 인벤토리가 M2 에서 확정된 뒤 회전 runbook 을 완성.
M4 (CD 연동, Medium)  ← M2 + M3 완료 후
  └─ 시크릿 사전 검증·버전드 교체 배포는 M2 의 시크릿·M3 의 회전 규약에 의존.
```

권장 실행 순서: **M1 → M2 → (M3 · M4 순차, M3 일부는 M2 와 병렬)**.

---

## M1 — 설정 계층 재설계 (BaseSettings 전환)

> [HARD] characterization-first: 리팩터링 전에 현행 동작을 특성화 테스트로 고정한다(REQ-105).

기술적 접근:

- **특성화 테스트 선작성 (REQ-105)**: 현재 `app/core/config.py` 가 노출하는 값
  (SECRET_KEY, ALGORITHM, ACCESS/REFRESH_TOKEN_EXPIRE_SECONDS, APP_ENV, SENTRY_DSN, FERNET_KEY)과
  import 시점 부작용(`database.engine`, `redis_client`, `celery_app`, firebase init)이 현재와
  동일한지 스냅샷하는 테스트를 먼저 만든다. 이 테스트는 리팩터링 전/후 모두 통과해야 한다.
- **BaseSettings 재작성 (REQ-101)**: `config.py` 를 `class Settings(BaseSettings)` 로 재작성하고
  `model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_file=".env", extra="ignore")`
  를 설정한다. 모듈 하단에 `settings = Settings()` 싱글턴을 노출한다. env-var > secrets_dir 우선순위를
  보장해 `tests/conftest.py` 의 `os.environ.setdefault` 주입이 그대로 유효하게 한다.
- **소비처 리팩터링 (REQ-102)**: `from app.core.config import SECRET_KEY` 형태를
  `from app.core.config import settings` → `settings.SECRET_KEY` 로 바꾼다. 대상: `security.py`,
  `auth_service.py`, `main.py`, `exceptions.py`, `api/auth.py`, `utils/redis/auth.py`,
  `database.py`, `redis_client.py`, `celery_app.py`(루트), `alembic/env.py`. import 시점 부작용
  (`create_engine`, `Redis.from_url`, `Celery(...)`, firebase `initialize_app`)은 settings 값을
  받도록만 바꾸고 동작은 보존한다.
- **REDIS_URL 통일 (REQ-103)**: `redis_client.py:9` 와 `celery_app.py:9` 의 중복 기본값을 제거하고
  `settings.REDIS_URL` 하나만 참조한다. 루트 `celery_app.py` 가 `app.core.config` 를 import 하게
  되므로 전이 import 경로를 확인한다(worker 태스크가 이미 app 모듈을 import 하므로 순환 위험 낮음).
- **Sentry 선택 처리 (REQ-104)**: `SENTRY_DSN: str = ""` 기본값으로 두어 secrets_dir 파일/ env 부재
  시 빈 문자열로 degrade. `main.py` 의 Sentry 초기화 가드(빈 DSN 시 미초기화)를 유지·검증한다.
- **필드 타입 정책 (확정: plain `str` 유지)**: 시크릿 필드를 pydantic `SecretStr` 로 감싸지 않고
  plain `str` 로 둔다. 이렇게 하면 `security.py:22` 의 `Fernet(FERNET_KEY.encode())` 를
  `Fernet(settings.FERNET_KEY.encode())` 로 최소 변경만 하면 되고, 소비처가 `.get_secret_value()`
  호출로 오염되지 않는다(blast radius 최소화). 로그 노출 통제는 코드 리뷰 및 evaluator 보안 점검에
  위임한다 — 리팩터링 시 시크릿이 로깅 경로에 흘러가지 않는지 리뷰 체크리스트로 확인한다.

## M2 — Docker secrets 전환

기술적 접근:

- **신규 시크릿 정의 (REQ-106)**: `docker-stack.yml` 최상위 `secrets:` 에 소문자 external 이름
  `kmc_secret_key_v1`, `kmc_fernet_key_v1`, `kmc_database_url_v1` 을 `external: true` 로 추가
  (firebase 패턴 계승). 사전 등록: `docker secret create kmc_secret_key_v1 ./secrets/secret_key.txt`
  등. **REDIS_URL 은 `.env.prod` 잔류(확정, 시크릿화 제외)** — 자격 증명 없는 내부 오버레이 DNS 이기
  때문. 추후 Redis 인증 도입 시 시크릿화는 별도 SPEC 에서 다룬다.
- **서비스 마운트 + 파일명 정합 (REQ-107) — 소문자 external + `target` 매핑 확정**:
  `api`·`celery_worker`·`celery_beat` 에 신규 시크릿을 마운트하되, 서비스 `secrets:` 의 `target:`
  으로 컨테이너 내 파일명을 pydantic 필드명에 정렬한다. 예:
  ```yaml
  secrets:
    - source: kmc_secret_key_v1   # external 이름(버전 suffix 포함)
      target: SECRET_KEY          # /run/secrets/SECRET_KEY (필드명, 불변)
  ```
  pydantic-settings 의 `SecretsSettingsSource` 는 secrets_dir 파일명이 필드명과 일치해야 읽으므로
  `target` = 필드명 규약으로 정합한다. **핵심 이점**: 버전 suffix 는 `source`(external 이름)에만 붙고
  `target` 은 불변이라, 회전 시 `source` 만 바꾸면 되고 앱 코드/필드명은 회전에 무관해진다.
- **.env.prod 민감 키 이관 (REQ-108)**: `.env.prod.example` 에서 SECRET_KEY/FERNET_KEY/DATABASE_URL
  을 제거하고 "Docker secret 으로 관리" 주석으로 대체. 남는 비시크릿 설정(ALGORITHM, 토큰 만료,
  APP_ENV, `REDIS_URL`)과 시크릿 인벤토리만 문서화. `REDIS_URL` 은 명시적으로 `.env.prod` 에 잔류.
- **마이그레이션 컨테이너 시크릿 주입 (REQ-109) — 일회성 Swarm 서비스 확정**: `deploy_migrate.sh` 의
  `docker run --env-file .env.prod` 는 Swarm secret 을 마운트하지 못하므로 **일회성 Swarm 서비스**로
  개편한다.
  - 실행: `docker service create --name kmc_migrate_$SHA --restart-condition=none
    --secret source=kmc_database_url_v1,target=DATABASE_URL --network shared_network_prod
    "$IMAGE_TAG" alembic upgrade head` — secrets_dir(`/run/secrets/DATABASE_URL`)가 정상 마운트됨.
  - 종료 코드 확인: 서비스 완료를 폴링(`docker service ps` state/`docker service logs`)해 태스크
    exit code 를 확인하고, 성공(0)이 아니면 `docker stack deploy` 를 진행하지 않는다. 확인 후
    `docker service rm` 으로 정리.
  - 결정 근거(대안 배제): 매니저 노드 파일 bind mount 는 노드 파일 관리 부담, 파일→`--env` 주입은
    프로세스 env/히스토리 노출 위험 때문에 배제하고 일회성 Swarm 서비스를 채택했다.
  - IF 컨테이너가 DATABASE_URL 을 못 얻거나 태스크가 실패하면 fail-fast(빈/오설정 대상 방지).

## M3 — GitHub 시크릿 관리 자동화

기술적 접근:

- **인벤토리 문서화 (REQ-110)**: `docs/deployment.md` 에 GitHub Actions 저장소 시크릿 표(이름·용도·
  소유자·회전 주기)를 추가. 현행: `SWARM_SSH_HOST/USER/KEY`(cd.yml 이 사용). 신규 도입 시 추가.
- **등록 스크립트/절차 (REQ-111)**: `gh secret set SWARM_SSH_KEY < deploy_key` 등 `gh` 기반 등록
  절차를 스크립트(예: `scripts/setup_github_secrets.sh`) 또는 문서 절차로 제공. UI 수동 등록 대체.
- **회전 runbook (REQ-112)**:
  - SSH 배포 키: 새 키페어 생성 → 매니저 노드 `authorized_keys` 갱신 → `gh secret set SWARM_SSH_KEY`
    → 배포 스모크 → 구 키 제거.
  - Docker secret(버전드 external 이름 + target 불변): `docker secret create kmc_secret_key_v2 …` →
    `docker-stack.yml` 의 `source: kmc_secret_key_v1` 을 `source: kmc_secret_key_v2` 로 교체(`target:
    SECRET_KEY` 는 그대로) → `docker stack deploy` → 롤아웃 완료 후 `docker secret rm kmc_secret_key_v1`.
    start-first 라 롤링 중 구 레플리카는 `kmc_secret_key_v1`, 신 레플리카는 `kmc_secret_key_v2` 를
    각자 마운트하되 둘 다 `/run/secrets/SECRET_KEY` 로 보이므로 **앱 코드는 회전에 무관** → 무중단.
    (단, 두 값이 동시에 유효해야 하는 시크릿(예: JWT 서명 키 교체)은 grace 기간 동안 신·구 키 병행
    검증 전략이 필요 — runbook 에 주의 표기.)

## M4 — CD 파이프라인 연동

기술적 접근:

- **시크릿 사전 검증 (REQ-113)**: `cd.yml` 의 deploy 스크립트(또는 `deploy_migrate.sh` 앞단)에서
  `docker secret ls --format '{{.Name}}'` 로 필요한 시크릿 존재를 확인. 누락 시 `exit 1` 로 배포
  중단(무한 재시도 금지, SPEC-INFRA-001 의 fail-fast 철학 계승).
- **버전드 교체 배포 (REQ-114)**: 회전 시 `docker-stack.yml` 의 secret `source` 가 `_v2` 로 바뀐
  상태로(단 `target` 은 불변) `docker stack deploy` 가 실행되며 `order: start-first` 를 유지하도록
  보장. 앱 코드 변경 없이 회전이 완결된다. IMAGE_TAG 주입 흐름(SPEC-INFRA-001)과 동일 패턴.
- **문서 갱신 (REQ-115)**: `docs/deployment.md` 에 시크릿 생성·인벤토리·회전 runbook·사전 검증
  단계를 통합 기술. SPEC-INFRA-001 의 배포 리허설 절과 정합.

---

## 리스크 및 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| 소비처 전면 리팩터링 회귀 | 인증·암호화·DB·Celery 파손 | REQ-105 특성화 테스트로 현행 동작 스냅샷 후 리팩터링, env-var 우선순위 유지 |
| import-time 시크릿 부재 | 앱/워커/beat/alembic 전면 기동 불가 | secrets_dir + env fallback, 명확한 검증 에러, 로컬 `.env` 유지 |
| secrets_dir 파일명 불일치 | 시크릿 미로딩 → 잘못된 값/기동 실패 | `secrets.target` = 필드명 규약 확정(REQ-107), 배포 리허설에서 실마운트 검증 |
| 마이그레이션 컨테이너 시크릿 미주입 | 배포 전 마이그레이션 실패 → 배포 중단 | 일회성 Swarm 서비스 확정(REQ-109), 종료 코드 확인 + fail-fast 로 오설정 대상 차단 |
| 필드 타입 str 유지 시 로그 노출 | 시크릿이 로그/트레이스에 평문 노출 | plain str 확정(SecretStr 미채택), 로그 노출은 코드 리뷰·evaluator 보안 점검으로 통제 |
| 회전 시맨틱 오해(값만 교체) | 조용한 미반영(재배포 no-op) | 버전드 external 이름 교체(target 불변) runbook 강제(REQ-112), 주의 표기 |
| celery_beat 전이 import 로 신규 시크릿 요구 | beat 기동 실패 | api/worker 와 동일하게 beat 에도 신규 시크릿 마운트(REQ-107) |
| 회전 중 서명 키 불연속 | 진행 중 토큰 검증 실패(간헐 401) | 서명 키류는 grace 기간 신·구 병행 검증 전략을 runbook 에 명시 |
| GitHub 시크릿 과다 권한/유출 | 매니저 노드 침해 | 최소 권한 배포 계정, 키 회전 runbook, `gh` 기반 등록으로 UI 실수 감소 |

## 전문가 상담 권장 (Conditional)

- **expert-devops**: Swarm secret 정의·마운트·버전드 회전, 마이그레이션 일회성 서비스 설계,
  GitHub Actions 시크릿 사전 검증 게이트 (M2, M3, M4).
- **expert-security**: plain str 유지 결정에 따른 로그 노출 통제 검토, JWT 서명 키 회전 grace 기간
  전략, `.env.prod` 잔류(REDIS_URL) 결정의 재확인 (M1, M3).
- **expert-backend**: BaseSettings 전환 시 import-time 부작용(엔진/브로커/firebase) 보존과
  특성화 테스트 설계 (M1).
