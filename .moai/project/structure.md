# KMCBeauty — 코드베이스 구조

> 레이어드 아키텍처 기반 FastAPI 애플리케이션

---

## 최상위 디렉터리

```
kmcbeauty-fastapi/
├── app/                  # 애플리케이션 소스 코드 (진입점: app/main.py)
├── alembic/              # DB 마이그레이션 (PostgreSQL 단일 베이스라인)
├── tests/                # pytest 테스트 (DB-free 재현·회귀 테스트, tests/fake_sql.py 헬퍼)
├── scripts/              # 배포·기동 스크립트 (git 실행 비트 100755 — 가드 테스트로 강제)
├── worker/               # Celery 작업 모듈
├── docs/                 # 운영 문서 (deployment.md, scaling.md)
├── docker/               # 환경별 Compose 파일
│   └── compose/
│       └── compose.dev.yml  # 로컬 개발용 (api 핫리로드 + Redis, worker 는 --profile; DB 는 호스트 PostgreSQL)
├── .github/
│   └── workflows/        # GitHub Actions (ci.yml, cd.yml)
├── celery_app.py         # Celery 애플리케이션 팩토리
├── docker-stack.yml      # Docker Swarm 배포 스택
├── Dockerfile            # 단일 스테이지 컨테이너 빌드
├── requirements.txt      # 의존성 목록 (전이 의존성까지 고정)
├── .env.example          # 환경변수 문서화 템플릿 (로컬)
├── .env.prod.example     # 운영 환경변수 템플릿 (.env.prod 생성 기준)
└── .pre-commit-config.yaml  # ruff lint+format pre-commit 훅
```

---

## app/ 레이어 구조

레이어 순서: **api → services → crud → models**

```
app/
├── api/                  # 라우터 레이어 (8개 라우터)
│   ├── auth.py
│   ├── user.py
│   ├── shop.py
│   ├── phonebook.py
│   ├── treatment.py
│   ├── treatment_menu.py     # 메뉴 + 세부 항목 (/treatment-menus/{menu_id}/details)
│   ├── device_push_token.py
│   └── summary.py
│
├── docs/                 # OpenAPI 태그·공통 응답, /changelog 라우터(api_change.py)
├── services/             # 비즈니스 로직 레이어
├── crud/                 # DB 접근 레이어
│
├── models/               # SQLAlchemy ORM 모델 (10개 테이블)
│   ├── mixin/
│   │   ├── timestamp.py     # TimestampMixin — created_at, updated_at
│   │   └── soft_delete.py   # SoftDeleteMixin — deleted_at 기반 논리 삭제
│   └── (user, shop, shop_user, shop_invite, phonebook, treatment, treatment_item,
│        treatment_menu, treatment_menu_detail, device_push_token)
│
├── schemas/              # Pydantic v2 요청/응답 스키마
├── dependencies/         # FastAPI 의존성 주입
│   ├── auth.py           # OAuth2 / JWT 검증 (access 토큰만, 소프트삭제 유저 차단)
│   └── shop.py           # 선택 샵 컨텍스트 — shop_user 멤버십 검증 (@MX:ANCHOR)
│
├── core/                 # 애플리케이션 설정·인프라
│   ├── config.py         # 중앙 settings 싱글톤 (@MX:ANCHOR) — BaseSettings(secrets_dir="/run/secrets"), env-var 우선, .env fallback; 모든 소비처가 이 객체에서 값을 읽음
│   ├── security.py       # argon2 해시 + bcrypt 레거시, JWT(python-jose)
│   ├── logging.py        # 로그 설정
│   ├── sentry.py         # Sentry SDK 초기화 (send_default_pii=False)
│   ├── rate_limit.py     # slowapi 속도 제한 (저장소 RATE_LIMIT_STORAGE_URI)
│   ├── redis_client.py   # Redis 연결 관리
│   ├── firebase.py       # firebase-admin 초기화
│   ├── limits.py         # 목록 조회 상한 상수
│   └── permissions.py    # 권한 헬퍼 (현재 호출부 없음)
│
├── utils/                # 유틸리티 모듈
│   ├── datetime.py       # 날짜/시간 처리
│   ├── email.py          # 이메일 소문자 정규화 (저장·조회 공통 단일 소스)
│   ├── phone.py          # 전화번호 정규화
│   ├── query.py          # 공통 쿼리 빌더 (KST 날짜 범위 필터 등)
│   └── redis/            # Redis 헬퍼 (auth 토큰, user 캐시, 선택 샵, dashboard 캐시)
│
└── enum/                 # 열거형 상수 (시술 상태·결제 수단·역할)
```

---

## 레이어 책임 분리

| 레이어 | 파일 위치 | 책임 |
|--------|----------|------|
| Router (api/) | 요청 파싱, 응답 직렬화, 인증 의존성 주입 |
| Service (services/) | 비즈니스 규칙, 트랜잭션 조율, 도메인 유효성 검증 (교차 테넌트 참조 검증 포함) |
| CRUD (crud/) | SQLAlchemy 쿼리, 소프트 삭제 필터, 페이지네이션 |
| Model (models/) | ORM 테이블 정의, Mixin 상속 |
| Schema (schemas/) | Pydantic v2 I/O DTO, 필드 검증 |

---

## 비동기 작업 구조

```
celery_app.py                  # Celery 브로커·백엔드 설정 (Redis)
worker/
└── tasks/
    └── treatment_task.py      # auto_complete_treatment
                               # — Celery beat 30분 주기
                               # — 방문(VISITED) 처리 + 종료 시각 경과 예약을 COMPLETED 로 (RESERVED 제외)
                               # — 완료 후 상점 대시보드 캐시 무효화
```

---

## 데이터베이스 마이그레이션

```
alembic/
├── env.py          # SQLAlchemy 연결 설정 (settings.DATABASE_URL)
├── versions/       # PostgreSQL 단일 베이스라인 5506c8cf9e2f
└── alembic.ini     # 마이그레이션 설정
```

- 대상 DB: PostgreSQL 16 (드라이버: psycopg 3)
- 로컬에서는 컨테이너 안에서 실행: `docker compose -f docker/compose/compose.dev.yml exec api alembic upgrade head`
- 주의: 베이스라인이 하나뿐이라 `alembic downgrade -1` 은 모든 테이블·ENUM 을 삭제한다

---

## 테스트 구조

```
tests/
├── conftest.py                     # 앱 임포트 전 환경변수 주입 + 대시보드 캐시 Redis 를 메모리 구현으로 격리
├── fake_sql.py                     # DB-free 테스트용 가짜 세션 (조건식을 메모리 행에 평가)
├── test_config_*.py                # Settings 특성화·env 우선순위
├── test_*_security.py              # 보안·인가 회귀 (테넌트 격리, 권한 상승, 푸시 인가, 인증 하드닝 등)
├── test_*_fix.py                   # 버그 재현·회귀 (PG 호환, 집계, 자동완료, 캐시, 멤버십 등)
├── test_*_perf.py                  # 성능 가드 (목록 상한 등)
├── test_api_contract_endpoints.py  # API 계약 테스트
├── test_infra_guards.py            # 배포 산출물 가드 (:latest 금지, 스크립트 실행 비트)
└── test_rate_limit_storage.py      # rate limit 저장소 설정
```

테스트 파일 35개 (conftest.py, fake_sql.py 제외). 테스트는 DB 를 모킹하며 CI 에 PostgreSQL 은 없다 — PostgreSQL 호환은 로컬 실DB 스모크(롤백)로 확인한다.

---

## 배포 스크립트

| 파일 | 용도 |
|------|------|
| `scripts/start_swarm.sh` | Swarm init + overlay 네트워크 생성 + 스택 배포 |
| `scripts/start_local.sh` | 호스트에서 uvicorn 직접 실행 (기본 로컬 경로는 compose) |
| `scripts/stop.sh` | 서비스 중지 |
| `scripts/deploy_migrate.sh` | 배포 전 Alembic 마이그레이션 실행 (일회성 Swarm 서비스로 시크릿 마운트, manager 노드 고정) |
| `scripts/setup_github_secrets.sh` | `gh secret set` 기반 GitHub Actions 시크릿 등록·갱신 자동화 (선택 `SWARM_SSH_FINGERPRINT` 포함) |
