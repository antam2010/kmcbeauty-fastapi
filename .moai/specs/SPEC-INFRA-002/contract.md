# SPEC-INFRA-002 Sprint Contract (Phase 2.0)

> evaluator-active 가 구현 전(Phase 2.0)에 작성한 계약. 이 문서가 최종 평가(Phase 2.8a)의
> 유일한 채점 기준이다. 계약 조건을 충족하지 못한 항목은 이유 없이 FAIL 처리된다.
> **협상 라운드 소진. 이 계약은 최종이다.**

---

## 프로파일

| 항목 | 값 |
|---|---|
| Harness | thorough |
| Evaluator Profile | strict |
| 우선순위 차원 | Security (35%) > Functionality (35%) > Craft (20%) > Consistency (10%) |
| 전체 FAIL 트리거 | Security ANY finding / 임의 차원 < 80% / UNVERIFIED 기준 존재 |
| Craft 커버리지 임계 | app/core/config.py (신규 재작성) >= 90% |
| 기존 테스트 기준선 | 76 PASS (5 pre-existing failures 제외: test_api_contract_endpoints 2건, test_list_query, test_soft_delete, test_user_privilege) |

---

## 1. Done 기준 (마일스톤별 runnable 테스트)

### M1 — 설정 계층 재설계 (T-101 ~ T-103)

#### 1-A. T-101: 특성화 테스트 선작성 — 리팩터링 전 GREEN 기준점

**파일**: `tests/test_config_characterization.py` (신규)

**필수 어설션 (8항목 전부 리팩터링 전 GREEN, T-103 완료 후 항목 8은 RED 전환)**:

1. 현행 `config.py` 모듈 레벨에서 `SECRET_KEY`, `ALGORITHM`, `APP_ENV`, `SENTRY_DSN`, `FERNET_KEY` 가 conftest 주입값과 일치함
2. `ACCESS_TOKEN_EXPIRE_SECONDS`, `REFRESH_TOKEN_EXPIRE_SECONDS` 가 `int` 타입으로 반환됨 (`int()` 캐스팅 현행 동작 스냅샷)
3. **[Sentry 특성화 — 방향 주의]** `SENTRY_DSN=""` 일 때 `app.main` 모듈 import 경로 전체가 예외 없이 통과함. `init_sentry` 를 모킹하지 않고 실제 호출을 통과시킨다 (main.py:36 은 조건부 가드 없이 무조건 호출함 — "init_sentry 가 호출되지 않는다" 는 틀린 어설션임)
4. `app.core.security` import 시 `Fernet(FERNET_KEY.encode())` 가 성공함 (`security.fernet` 객체가 `Fernet` 인스턴스임을 확인)
5. `app.database` import 시 `engine` 객체가 `None` 이 아님 (SQLAlchemy engine import-time 생성 확인)
6. `app.core.redis_client` import 시 `redis_client` 객체가 `None` 이 아님
7. `celery_app` (저장소 루트 모듈) import 시 `celery_app` 객체가 `None` 이 아님 (repo-root 임포트 경로 확인)
8. `redis_client.py` 와 `celery_app.py` 각각에 `REDIS_URL` 기본값 `"redis://redis:6379/0"` 이 독립적으로 하드코딩되어 있음 — **T-103 완료 후 이 어설션은 FAIL 전환이 정상** (T-101은 리팩터링 전 현행 동작 스냅샷임)

**검증 명령**:
```bash
# 리팩터링 전 기준점 확인
.venv/bin/pytest tests/test_config_characterization.py -v  # 항목 1~8 모두 GREEN
```

---

#### 1-B. T-102: BaseSettings 재작성 후 단위 테스트

**파일**: `tests/test_config_settings.py` (신규)

**필수 테스트 케이스 (6항목 전부 GREEN)**:

1. **AC-2 env-var 우선**: `tmp_path` 에 `SECRET_KEY` 파일 생성 + env var `SECRET_KEY=env-val` 설정 → `Settings(secrets_dir=str(tmp_path)).SECRET_KEY == "env-val"`
2. **AC-2 역방향 (file fallback)**: env var `SECRET_KEY` 없음 + `tmp_path/SECRET_KEY` 파일에 `"file-val"` → `Settings(secrets_dir=str(tmp_path)).SECRET_KEY == "file-val"`
3. **AC-4 Sentry degrade**: `SENTRY_DSN` env var 없음 + secrets_dir 에도 `SENTRY_DSN` 파일 없음 → `settings.SENTRY_DSN == ""` (빈 문자열, 예외 없음)
4. **/run/secrets 부재 환경 호환**: `Settings(secrets_dir="/run/secrets")` 초기화 시 (필수 env var 세팅 상태에서) 예외 없음 — secrets_dir 경로 부재가 오류를 유발하지 않아야 함
5. **FERNET_KEY Fernet 소비**: `Fernet(settings.FERNET_KEY.encode())` 가 성공함 (plain str 유지 확정 — `.get_secret_value()` 불필요)
6. **REDIS_URL 단일 진실원천**: `settings.REDIS_URL` 이 접근 가능하고 기본값이 `"redis://redis:6379/0"` 임

**검증 명령**:
```bash
.venv/bin/pytest tests/test_config_settings.py -v
```

---

#### 1-C. T-103: 소비처 전환 후 grep 게이트 + 전체 스위트

리팩터링 전/후 양쪽에서 모두 통과해야 하는 특성화 테스트:
```bash
.venv/bin/pytest tests/test_config_characterization.py -v  # 항목 1~7 GREEN (항목 8은 RED 전환 확인)
```

소비처 전환 완료 검증:
```bash
# os.getenv 잔존 0건 확인
git grep -rn 'os\.getenv.*REDIS_URL' -- app/ celery_app.py          # 0건
git grep -rn 'os\.getenv.*SECRET_KEY' -- app/ celery_app.py alembic/  # 0건
git grep -rn 'os\.getenv.*DATABASE_URL' -- app/ celery_app.py alembic/ # 0건
git grep -rn 'os\.getenv.*FERNET_KEY' -- app/ celery_app.py           # 0건

# 직접 import 잔존 0건 확인 (settings 객체 임포트로 대체됨)
git grep -rn 'from app\.core\.config import [A-Z]' -- app/ celery_app.py alembic/  # 0건

# 전체 스위트 (76 기존 GREEN 유지 + 신규 테스트 GREEN)
.venv/bin/pytest -v --cov=app/core/config --cov-report=term-missing
```

---

### M2 — Docker secrets 전환 (T-104 ~ T-106)

#### 2-A. T-104: docker-stack.yml 정적 검증

```bash
# YAML 파싱 성공
python3 -c "import yaml; yaml.safe_load(open('docker-stack.yml')); print('YAML OK')"

# 최상위 secrets 정의 + 3개 서비스 마운트 + target 필드명 일치 검증
python3 << 'EOF'
import yaml
s = yaml.safe_load(open('docker-stack.yml'))

# 최상위 secrets: 신규 3건 + firebase 유지
top = set(s.get('secrets', {}).keys())
required_top = {'kmc_secret_key_v1', 'kmc_fernet_key_v1', 'kmc_database_url_v1', 'firebase_service_account'}
assert required_top <= top, f"Missing top-level secrets: {required_top - top}"

# external: true 확인
for k in ['kmc_secret_key_v1', 'kmc_fernet_key_v1', 'kmc_database_url_v1']:
    assert s['secrets'][k].get('external') is True, f"{k}: external must be true"

# 3개 서비스 target 검증 (실제 서비스명: kmcbeauty_api, celery_worker, celery_beat)
svcs = ['kmcbeauty_api', 'celery_worker', 'celery_beat']
expected_targets = {'SECRET_KEY', 'FERNET_KEY', 'DATABASE_URL'}
for svc in svcs:
    mounts = s['services'][svc].get('secrets', [])
    targets = {m['target'] for m in mounts if isinstance(m, dict) and 'target' in m}
    assert expected_targets <= targets, f"{svc}: missing targets {expected_targets - targets}"
    assert 'REDIS_URL' not in targets, f"{svc}: REDIS_URL must NOT be a Docker secret"

# source 이름이 소문자 버전드 패턴인지 확인
for svc in svcs:
    mounts = s['services'][svc].get('secrets', [])
    src_map = {m['target']: m['source'] for m in mounts if isinstance(m, dict) and 'target' in m}
    assert src_map.get('SECRET_KEY', '').startswith('kmc_secret_key_v'), f"{svc}: SECRET_KEY source must be kmc_secret_key_vN"

print("ALL CHECKS PASSED")
EOF
```

---

#### 2-B. T-105: AC-6 민감 값 평문 0건 게이트

```bash
# .env.prod.example 민감 키 값 행 없음 (주석/인벤토리만 허용)
python3 << 'EOF'
import re
content = open('.env.prod.example').read()
# 값 할당 행 패턴 (주석이 아닌 라인에서 실제 값 있음)
sensitive_patterns = [
    r'^SECRET_KEY\s*=\s*\S',
    r'^FERNET_KEY\s*=\s*\S',
    r'^DATABASE_URL\s*=\s*mysql',
    r'^DATABASE_URL\s*=\s*postgresql',
]
for pat in sensitive_patterns:
    match = re.search(pat, content, re.MULTILINE)
    assert match is None, f"Sensitive value found: {match.group()}"
print("OK: no sensitive values in .env.prod.example")
EOF

# .gitignore에 .env.prod 유지
grep -n '^\.env\.prod$' .gitignore && echo "OK" || echo "FAIL: .env.prod not in .gitignore"

# 저장소 추적 파일 전체 — 민감 실값 패턴 0건
git grep -rn 'SECRET_KEY\s*=\s*[^$#\s]' -- ':!tests/' ':!*.example' ':!*.md'
git grep -rn 'FERNET_KEY\s*=\s*[^$#\s]' -- ':!tests/' ':!*.example' ':!*.md'
```

---

#### 2-C. T-106: deploy_migrate.sh 정적 검증

```bash
# docker run --env-file 제거 확인 (0건)
count=$(grep -c 'docker run' scripts/deploy_migrate.sh 2>/dev/null || echo 0)
[ "$count" -eq 0 ] && echo "OK: no docker run" || echo "FAIL: docker run found ($count)"

count=$(grep -c 'env-file' scripts/deploy_migrate.sh 2>/dev/null || echo 0)
[ "$count" -eq 0 ] && echo "OK: no env-file" || echo "FAIL: env-file found ($count)"

# docker service create --secret 존재
grep -n 'docker service create' scripts/deploy_migrate.sh
grep -n 'restart-condition=none' scripts/deploy_migrate.sh
grep -n '\-\-secret.*DATABASE_URL\|DATABASE_URL.*\-\-secret' scripts/deploy_migrate.sh

# 종료 코드 확인 로직 존재 (docker service ps / inspect / rm)
grep -n 'docker service' scripts/deploy_migrate.sh | grep -E 'ps|inspect|logs|rm'

# --local 분기 DATABASE_URL 미설정 fail-fast (계약 요구사항 §계약 전제 위반 위험 1)
grep -A8 '\-\-local' scripts/deploy_migrate.sh | grep -E 'DATABASE_URL'

# 구문 검사
bash -n scripts/deploy_migrate.sh && echo "SYNTAX OK"
```

---

### M3/M4 — GitHub 자동화 및 CD 연동 (T-107 ~ T-110)

#### 3-A. T-109: cd.yml 정적 검증 — 사전 검증 위치

```bash
# YAML 파싱
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/cd.yml')); print('YAML OK')"

# 사전 검증이 마이그레이션 이전에 위치 (줄 번호 비교 — 계약 요구사항 §계약 전제 위반 위험 2)
python3 << 'EOF'
content = open('.github/workflows/cd.yml').read()
lines = content.split('\n')
pre_check_line = next((i for i, l in enumerate(lines) if 'docker secret ls' in l), -1)
migrate_line = next((i for i, l in enumerate(lines) if 'deploy_migrate.sh' in l), -1)
assert pre_check_line != -1, "FAIL: docker secret ls not found in cd.yml"
assert migrate_line != -1, "FAIL: deploy_migrate.sh not found in cd.yml"
assert pre_check_line < migrate_line, \
    f"FAIL: pre-check (line {pre_check_line}) must be BEFORE migrate (line {migrate_line})"
print(f"OK: pre-check at line {pre_check_line}, migrate at line {migrate_line}")
EOF

# abort 로직 존재
grep -n 'exit 1' .github/workflows/cd.yml
```

---

#### 3-B. T-107/T-108/T-110: 문서 완결성 검증

```bash
# GitHub Actions 시크릿 인벤토리 (SWARM_SSH_HOST/USER/KEY 용도·소유자 포함)
grep -n 'SWARM_SSH_HOST' docs/deployment.md
grep -n 'SWARM_SSH_USER' docs/deployment.md
grep -n 'SWARM_SSH_KEY' docs/deployment.md

# gh secret set 기반 등록 스크립트
test -f scripts/setup_github_secrets.sh && echo "FILE OK" || echo "FAIL: script not found"
grep -n 'gh secret set' scripts/setup_github_secrets.sh
bash -n scripts/setup_github_secrets.sh && echo "SYNTAX OK"

# Docker secret 회전 runbook: source 교체(target 불변) + grace 기간 주의 명시
grep -n 'target' docs/deployment.md | grep -i 'secret\|rotation\|불변\|immutable'
grep -n '_v2\|version\|kmc_secret_key_v' docs/deployment.md
grep -n 'grace\|병행\|동시\|JWT\|서명\|signing' docs/deployment.md

# Docker secret 생성 명령 존재
grep -n 'docker secret create' docs/deployment.md

# 배포 리허설 절과 정합
grep -n 'kmc_database_url\|kmc_secret_key\|kmc_fernet_key' docs/deployment.md
```

---

## 2. 필수 엣지 케이스 (테스트 커버 의무)

| # | 엣지 케이스 | 커버 방법 | 미커버 시 결과 |
|---|---|---|---|
| EC-1 | import-time 읽기 순서: conftest env 주입 → settings=Settings() 초기화 | T-101: conftest 주입 후 app.* import 성공 확인 | 전체 스위트 RED |
| EC-2 | /run/secrets 없는 로컬 테스트 환경 | T-102 케이스 4: Settings(secrets_dir="/run/secrets") 예외 없음 | 로컬 개발 환경 기동 불가 |
| EC-3 | celery_app.py repo-root에서 app.core.config import | T-101 항목 7: celery_app import 성공 | Celery worker 기동 불가 |
| EC-4 | alembic/env.py → settings.DATABASE_URL (T-103 후) | T-103 grep 게이트: alembic/env.py 에 os.getenv("DATABASE_URL") 0건 | 마이그레이션 DB URL 없음 |
| EC-5 | secrets 파일명 대소문자 일치: target=SECRET_KEY(대문자) | T-104 python 스크립트: target 값 대소문자 정확 확인 | Silent 미로딩 → 잘못된 키 |
| EC-6 | 회전 no-op 함정: 같은 external 이름 값만 교체 → 재배포가 no-op | T-108 runbook: "source만 _v2 교체, target 불변" 명시 | 회전 반영 안 됨 (조용한 실패) |
| EC-7 | init_sentry 무조건 호출(main.py:36) — DSN="" 일 때 예외 없음 | T-101 항목 3: 모킹 없이 init_sentry 통과 확인 | Sentry 어설션 방향 오류 |
| EC-8 | --local 모드 DATABASE_URL env var 미설정 | T-106 정적: --local 분기에 DATABASE_URL 체크 존재 | 빈/None DB에 마이그레이션 실행 |

---

## 3. 하드 임계치 (Hard Thresholds)

| 기준 | 값 | 실패 결과 |
|---|---|---|
| 기존 테스트 기준선 | 76 PASS 유지 (동일 5건 pre-existing 제외) | Functionality FAIL |
| 신규 config 테스트 | test_config_characterization.py + test_config_settings.py GREEN | Functionality FAIL |
| 신규/수정 config 모듈 커버리지 | app/core/config.py >= 90% | Craft FAIL |
| 저장소 민감 값 평문 | git grep 패턴 0건 | **Security FAIL = Overall FAIL** |
| Security 차원 (strict profile) | 임의 심각도(Critical/High/Medium/Low/Info) 발견 0건 | **Overall FAIL** |
| 각 차원 최소 점수 (strict profile) | Functionality/Security/Craft/Consistency 각각 >= 80% | Overall FAIL |
| UNVERIFIED 기준 (strict profile) | 0건 | Overall FAIL |
| plain str 로그 노출 | 시크릿 값이 logging/print 경로에 직접 전달되지 않음 | Security FAIL |

**커버리지 범위 명시**:
- 적용 대상: `app/core/config.py` (신규 재작성), 수정된 app/*.py 모듈
- 비적용 대상: `docker-stack.yml`, `scripts/*.sh`, `.github/workflows/*.yml` — 코드 커버리지 불가하므로 정적 검증(grep/python 스크립트)으로 대체

---

## 4. 최종 평가 시나리오 (Phase 2.8a 실행 명령)

```bash
# === M1: 특성화 기준점 확인 ===
.venv/bin/pytest tests/test_config_characterization.py -v

# === M1: settings 단위 테스트 ===
.venv/bin/pytest tests/test_config_settings.py -v

# === M1: 전체 스위트 + config 커버리지 ===
.venv/bin/pytest -v --cov=app/core/config --cov=app --cov-report=term-missing

# === M1: os.getenv 잔존 검사 ===
git grep -rn 'os\.getenv.*REDIS_URL' -- app/ celery_app.py
git grep -rn 'os\.getenv.*SECRET_KEY' -- app/ celery_app.py alembic/
git grep -rn 'os\.getenv.*DATABASE_URL' -- app/ celery_app.py alembic/
git grep -rn 'os\.getenv.*FERNET_KEY' -- app/ celery_app.py
git grep -rn 'from app\.core\.config import [A-Z]' -- app/ celery_app.py alembic/

# === M2: docker-stack.yml 파싱 + target 검증 ===
python3 -c "import yaml; yaml.safe_load(open('docker-stack.yml')); print('YAML OK')"

python3 << 'EOF'
import yaml
s = yaml.safe_load(open('docker-stack.yml'))
top = set(s.get('secrets', {}).keys())
required_top = {'kmc_secret_key_v1', 'kmc_fernet_key_v1', 'kmc_database_url_v1', 'firebase_service_account'}
assert required_top <= top, f"Missing: {required_top - top}"
svcs = ['kmcbeauty_api', 'celery_worker', 'celery_beat']
expected_targets = {'SECRET_KEY', 'FERNET_KEY', 'DATABASE_URL'}
for svc in svcs:
    mounts = s['services'][svc].get('secrets', [])
    targets = {m['target'] for m in mounts if isinstance(m, dict) and 'target' in m}
    assert expected_targets <= targets, f"{svc}: {expected_targets - targets}"
    assert 'REDIS_URL' not in targets, f"{svc}: REDIS_URL must not be a secret"
print("M2 YAML CHECKS OK")
EOF

# === M2: AC-6 민감 값 평문 0건 ===
git grep -rn 'SECRET_KEY\s*=\s*[^$#\s]' -- ':!tests/' ':!*.example' ':!*.md'
git grep -rn 'FERNET_KEY\s*=\s*[^$#\s]' -- ':!tests/' ':!*.example' ':!*.md'
grep -n '^\.env\.prod$' .gitignore

# === M2: deploy_migrate.sh 정적 검사 ===
bash -n scripts/deploy_migrate.sh
grep -c 'docker run' scripts/deploy_migrate.sh          # 0이어야 함
grep -c 'docker service create' scripts/deploy_migrate.sh  # >=1이어야 함
grep -c 'restart-condition=none' scripts/deploy_migrate.sh # >=1이어야 함
grep -A8 '\-\-local' scripts/deploy_migrate.sh | grep 'DATABASE_URL'  # 체크 존재

# === M4: cd.yml 사전 검증 위치 ===
python3 -c "
c=open('.github/workflows/cd.yml').read(); ls=c.split('\n')
p=next((i for i,l in enumerate(ls) if 'docker secret ls' in l), -1)
m=next((i for i,l in enumerate(ls) if 'deploy_migrate.sh' in l), -1)
assert 0<p<m, f'FAIL pre-check({p}) must be before migrate({m})'
print(f'OK: pre-check@{p} < migrate@{m}')
"

# === M3: 문서 완결성 ===
grep -n 'SWARM_SSH_HOST\|SWARM_SSH_USER\|SWARM_SSH_KEY' docs/deployment.md
grep -n 'gh secret set' scripts/setup_github_secrets.sh
bash -n scripts/setup_github_secrets.sh
grep -n 'docker secret create kmc_' docs/deployment.md
grep -n '_v2' docs/deployment.md
```

---

## 5. 우선순위 차원

1. **Security** (35%, 최우선) — 평가 항목:
   - 민감 값 평문 노출 (git grep 게이트)
   - plain str 유지 결정 → 로그/트레이스 경로에 시크릿이 전달되지 않는지 코드 확인
   - docker-stack.yml `external: true` 누락 여부
   - deploy_migrate.sh에서 DATABASE_URL이 process env/히스토리에 노출되지 않음 (Swarm service 확정)

2. **Functionality** (35%) — 평가 항목:
   - AC-1~4 (로컬 검증 가능), AC-5·6·7·8·9 정적 게이트 통과
   - 76 기존 테스트 GREEN 유지

---

## 6. 로컬 검증 불가 항목 (라이브 유예)

| AC | 항목 | 이유 | 리허설 절차 참조 |
|---|---|---|---|
| AC-5 라이브 | Swarm 실제 `/run/secrets/<필드명>` 마운트 + 앱 정상 기동 | Docker Swarm 클러스터 필요 | acceptance.md AC-5 라이브 |
| AC-7 라이브 | 일회성 Swarm 서비스 `alembic upgrade head` E2E | Swarm + 실제 DB 접근 필요 | acceptance.md AC-7 라이브 |
| AC-9 라이브 | Docker secret 누락 시 cd.yml abort 동작 확인 | GitHub Actions 실행 환경 필요 | acceptance.md AC-9 |
| AC-10 | 버전드 시크릿 무중단 회전 + /health 폴링 0실패 | Swarm + 롤링 배포 환경 필요 | acceptance.md AC-10 |

라이브 유예 항목은 이 계약의 PASS/FAIL 판정에서 제외한다. 배포 리허설 절차(acceptance.md 해당 AC + docs/deployment.md)로 운영 환경에서 별도 검증한다.

---

## 7. 계약 전제 위반 위험 (구현자 필수 조치)

> 이 섹션의 각 항목은 현재 tasks.md 기술이 이 계약의 요구사항을 충족하지 못할 위험이다.
> 구현자가 반드시 수정해야 한다. 수정 없이 구현하면 해당 기준은 FAIL 처리된다.

### 위반 위험 1: T-106 — --local 모드 DATABASE_URL 부재 fail-fast 미구현

**문제**: T-106 task 설명이 container 경로(docker service create 전환)에만 집중하고, `--local` 분기의 DATABASE_URL 부재 fail-fast를 명시하지 않음.

**근거**: 오케스트레이터 바인딩 제약 — `--local` 모드는 DATABASE_URL env var 없으면 명확한 오류로 중단해야 함 (`.env.prod`에서 DATABASE_URL이 제거된 후 빈 값으로 마이그레이션 실행 방지).

**필수 수정**: T-106 구현 시 `--local` 분기에 추가:
```bash
if [ "$MODE" = "--local" ]; then
  : "${DATABASE_URL:?DATABASE_URL 환경변수를 설정하세요 (Docker secret 전환 후 .env.prod에 없음)}"
  echo "[MIGRATE] 로컬 alembic upgrade head 실행..."
  alembic upgrade head
fi
```

**계약 검증 게이트**: `grep -A8 '\-\-local' scripts/deploy_migrate.sh | grep 'DATABASE_URL'` → 1건 이상

---

### 위반 위험 2: T-109 — 사전 검증 단계 위치 모호성 (cd.yml 인라인 스크립트 필수)

**문제**: T-109 기술이 "cd.yml 의 deploy 스크립트(또는 deploy_migrate.sh 앞단)"라고 하여 `deploy_migrate.sh` 내부도 허용되는 것처럼 읽힘.

**근거**: 오케스트레이터 바인딩 제약 — 사전 검증은 SSH deploy 스크립트의 맨 앞(BEFORE deploy_migrate.sh)에 위치해야 함. `deploy_migrate.sh` 내부에 두면 마이그레이션 Swarm 서비스가 이미 기동 시도한 후에야 검증이 일어남 (실질적으로 BEFORE 보장 불가).

**필수 수정**: `cd.yml` SSH 인라인 스크립트(`script:` 블록)에서 `./scripts/deploy_migrate.sh` 호출 라인 이전에 위치:
```yaml
script: |
  set -euo pipefail
  cd "${DEPLOY_DIR:-/opt/kmcbeauty}"
  # --- 시크릿 사전 검증 (REQ-INFRA-113) ---
  REQUIRED_SECRETS="kmc_secret_key_v1 kmc_fernet_key_v1 kmc_database_url_v1"
  for s in $REQUIRED_SECRETS; do
    docker secret ls --format '{{.Name}}' | grep -qx "$s" || \
      { echo "ERROR: Docker secret '$s' not found. Aborting."; exit 1; }
  done
  # --- 여기서부터 기존 배포 흐름 ---
  ./scripts/deploy_migrate.sh
  docker stack deploy ...
```

**계약 검증 게이트**: Python 줄 번호 비교 스크립트 (`pre_check_line < migrate_line`)

---

### 위반 위험 3: T-104 — 서비스명 오기 (`api` vs `kmcbeauty_api`)

**문제**: spec.md, plan.md 전반에서 API 서비스를 `"api"` 로 지칭하나, `docker-stack.yml` 의 실제 서비스 키는 `kmcbeauty_api`.

**근거**: `api:` 키에 secrets 마운트를 추가하면 새 서비스가 생성되어 실제 `kmcbeauty_api` 서비스에 반영되지 않음.

**필수 수정**: T-104 구현 시 `kmcbeauty_api`, `celery_worker`, `celery_beat` 세 서비스명을 사용. `api` 키는 사용 금지.

**계약 검증 게이트**: T-104 python 스크립트에서 `svcs = ['kmcbeauty_api', 'celery_worker', 'celery_beat']` 기준으로 검증.

---

### 위반 위험 4: T-101 — Sentry 특성화 어설션 방향 오류 위험

**문제**: `app/main.py:36` 은 `init_sentry(dsn=SENTRY_DSN, ...)` 를 무조건 호출함 (조건부 가드 없음). 특성화 테스트 작성 시 "SENTRY_DSN 가 비어있으면 init_sentry 를 호출하지 않는다" 고 잘못 어설션할 수 있음.

**근거**: 이 어설션은 현재 코드 동작과 다르다 → 리팩터링 전 RED 기준점이 잘못됨 → 잘못된 동작을 "보존"하게 됨.

**필수 수정**: 올바른 특성화 어설션 — "SENTRY_DSN='' 일 때 app.main import 경로에서 예외 없음" + "init_sentry 를 모킹하지 않고 실제 sentry_sdk.init(dsn='') 호출이 완료됨". `sentry_sdk.init(dsn='')` 는 no-op 이므로 실제 SDK 호출을 허용한다.

---

### 위반 위험 5: T-103 — alembic/env.py double-load 위험

**문제**: `alembic/env.py` 는 현재 (1) `load_dotenv(.env.{env}, override=True)` 로 env 파일을 직접 로드하고, (2) `from app.database import DATABASE_URL` 로 settings-mediated 값을 받는다. T-103 에서 alembic 이 `settings.DATABASE_URL` 을 참조하도록 전환할 때, alembic 전용 `load_dotenv(..., override=True)` 가 남아 있으면 pydantic-settings 의 env-var > secrets_dir 우선순위와 충돌한다 (`override=True` → dotenv 가 OS env var 를 덮어쓸 수 있음).

**필수 수정 (택 1)**:
- 권장: alembic/env.py 에서 `load_dotenv` 호출을 제거하고 `from app.core.config import settings` 만 사용. settings 의 `env_file=".env"` 설정이 `.env` 로딩을 담당.
- 차선: alembic/env.py 에서 `override=True` 를 `override=False` 로 변경 (OS env var 우선 유지).

**계약 검증 게이트**: `grep -n 'load_dotenv.*override=True' alembic/env.py` → 0건 (권장 경로) 또는 `grep -n 'override' alembic/env.py` 가 `False` 를 사용.

---

## 서명

이 계약은 evaluator-active 가 Phase 2.0 에서 작성했다.
구현자(manager-ddd/tdd)는 이 계약의 조건을 모두 충족해야 한다.
Phase 2.8a 최종 평가는 이 계약의 §4 테스트 시나리오를 기준으로 채점한다.

작성일: 2026-07-07
Harness: thorough
Profile: strict
최종 협상 라운드: 0/1 소진
