# KMCBeauty — 기술 스택 및 인프라

---

## 런타임 환경

| 항목 | 버전 / 값 |
|------|----------|
| Python | 3.13-slim (Dockerfile 기준) |
| ruff target-version | py313 |
| 컨테이너 베이스 이미지 | `python:3.13-slim` |
| 실행 사용자 | non-root (Dockerfile 설정) |
| 컨테이너 타임존 | `TZ=Asia/Seoul` (로그 시각용 — DB 저장 규약은 UTC) |

---

## 핵심 프레임워크

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| FastAPI | 0.139 | Web API 프레임워크 |
| Uvicorn | 0.49 | ASGI 서버 |
| uvloop | — | 고성능 이벤트 루프 |
| httptools | — | HTTP 파서 (uvicorn 성능 최적화) |
| orjson | — | 고속 JSON 직렬화 |

---

## 데이터베이스 & ORM

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| SQLAlchemy | 2.0.51 | ORM (동기 세션) |
| Alembic | 1.18.5 | DB 스키마 마이그레이션 |
| psycopg (+ psycopg-binary) | 3.3.6 | PostgreSQL 드라이버 (`postgresql+psycopg://`) |

- 대상 DB: **PostgreSQL 16** (2026-10 MySQL 에서 전환 — MySQL 호환·데이터 이관 없음)
  - 로컬 개발: Windows 호스트의 PostgreSQL 을 컨테이너에서 `host.docker.internal:5432` 로 접근 (DB `kmcbeauty`).
    WSL 셸에서는 방화벽으로 접근할 수 없으므로 alembic 등 DB 작업은 컨테이너 안에서 실행한다.
- 마이그레이션: PostgreSQL 단일 베이스라인 `5506c8cf9e2f` (MySQL 시절 7개 revision 대체)
- 세션 타임존 UTC 고정 (`connect_args options -c timezone=UTC`) — naive DateTime 컬럼은 UTC wall-clock 저장 규약
- 이메일은 앱 레벨에서 소문자로 정규화 (`app/utils/email.py`) — PostgreSQL 은 문자열 비교가 대소문자 구분

---

## 데이터 검증 & 설정

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| Pydantic | 2.13 | 요청/응답 스키마 검증 |
| pydantic-settings | 2.14.2 | 설정 관리 — `BaseSettings(secrets_dir="/run/secrets")`, env-var 우선, `.env` fallback |

---

## 인증 & 보안

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| python-jose | 3.5.0 | JWT 토큰 생성·검증 (`algorithms` 고정) |
| argon2-cffi | — | 기본 비밀번호 해시 알고리즘 |
| passlib | — | 비밀번호 해시 추상화 (bcrypt 레거시 지원) |
| bcrypt | 4.0.1 (pinned `<4.1`) | 레거시 해시 지원 (passlib 호환성) |
| cryptography (Fernet) | — | 대칭 암호화 |

> **주의:** bcrypt는 passlib 호환성 문제로 `4.0.1`로 고정됨. 업그레이드 시 검증 필요.

- bearer 인증은 액세스 토큰만 허용 (`type == "access"` 검증), 토큰 재발급(`/auth/refresh`)은 리프레시 토큰만 허용 (`type == "refresh"`)
- 소프트삭제(탈퇴) 유저는 즉시 401. 멤버십 행은 남지만 멤버 판정·담당자·푸시 대상·직원 목록에서 제외되고, 탈퇴 시 디바이스 토큰 비활성화
- 세션 정리(유저 캐시·리프레시 토큰 삭제)는 탈퇴·정보 수정의 DB 커밋이 성공한 뒤에만 수행. 로그아웃 응답은 토큰을 돌려주지 않음
- 샵 데이터 접근은 멤버십(`shop_user`) 기준이며, 예약의 고객·담당자·메뉴 상세 참조는 현재 샵 소속만 허용
- 초대 코드는 1회용: 가입 트랜잭션에서 `SELECT … FOR UPDATE` 로 잠근 뒤 소비(삭제) — 동시 가입은 두 번째가 실패
- 비밀번호 최소 8자 (가입·변경 스키마만, 로그인 폼은 길이 검사 없음)

### 입력 검증 · 오류 매핑

- 요청 스키마의 문자열 길이·숫자 범위를 DB 컬럼에 맞춤 (`app/schemas/limits.py`) — 초과 시 DataError 500 대신 422
- 조회 날짜 필터 1900-01-01 ~ 2100-12-31, 페이지 번호 1 ~ 100000 (`app/schemas/pagination.py`, size 상한 100)
- DB 값 오류(`sqlalchemy.exc.DataError` — NUL 바이트, 범위 초과 등)는 400 `{DOMAIN}_BAD_REQUEST` + 일반 문구:
  `CustomException` 생성 시 원인 예외를 보고 5xx 를 400 으로 바꾸고, 서비스가 감싸지 않은 DataError 는
  `app/main.py` 의 전역 핸들러가 처리한다. 원문은 응답에 싣지 않고 경고 로그로만 남기며 Sentry 로 보내지 않는다.

---

## 캐시 & 비동기 작업

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| Redis 클라이언트 | 6.4 | 캐싱, 속도 제한 카운터(운영), Celery 브로커 |
| Celery | 5.6.3 | 비동기 태스크 큐 + beat 스케줄러 |

Celery 작업:
- `auto_complete_treatment`: 30분 주기. 방문(VISITED) 처리된 예약 중 종료 시각(`reserved_at` + 항목 소요시간 합)이
  지난 것을 COMPLETED 로 전환한다. 처리되지 않은 RESERVED 는 대상이 아니다(노쇼가 실매출로 잡히는 것 방지 —
  직원이 방문/노쇼를 고른다). 완료 처리 후 해당 상점의 대시보드 캐시를 무효화한다.

대시보드 캐시: 키에 기간 종류(day/month)와 상점 버전을 포함하고, 예약 생성·수정·취소·자동완료 커밋 후 버전을 올려 무효화한다 (TTL 30분).

---

## 추가 기능 라이브러리

| 라이브러리 | 버전 | 용도 |
|-----------|------|------|
| slowapi | 0.1.10 | 요청 속도 제한 — 저장소 `RATE_LIMIT_STORAGE_URI` (기본 `memory://`, 운영은 공유 Redis DB 6). IP 기준 분당: 로그인 5 · 가입 5 · 이메일 확인 20 · 재발급 30 · 초대 생성 10 · 푸시 발송 10 |
| limits | 5.8.0 | slowapi 저장소 백엔드 |
| fastapi-pagination | 0.15.15 | 페이지 기반 페이지네이션 |
| firebase-admin | 7.5 | FCM 푸시 알림 발송 (같은 샵 멤버 대상만 허용) |
| Sentry SDK | 2.64 | 에러 추적 및 성능 트레이싱 |

Sentry 설정: `traces_sample_rate=0.2`, `send_default_pii=False`(요청 쿠키·본문 자동 첨부 차단), `include_local_variables=False`(프레임 지역변수 미전송), `EventScrubber` 기본 목록 + `refresh_token`/`x-refresh-token`/`x_refresh_token`(재귀), 사용자 식별은 id 만 전송, 로컬·디버그 환경에서 비활성화.

API 문서: `/docs` · `/redoc` · `/openapi.json` · `/changelog` 는 `APP_ENV=production` 에서 비공개 (`app/main.py` `api_docs_kwargs`), local/debug/stage 는 공개.

---

## 개발 도구

| 도구 | 버전 | 용도 |
|------|------|------|
| ruff | 0.14.0 | 린트 + 포맷 (line-length 88, target-version py313) |
| pytest | 8.4.2 | 테스트 프레임워크 |
| pytest-cov | 6.1.1 | 커버리지 측정 (report-only, --fail-under 게이트 없음) |
| coverage | 7.6.10 | 커버리지 리포트 백엔드 |
| pre-commit | — | ruff lint+format 커밋 훅 (`.pre-commit-config.yaml`) |

---

## 인프라

### 운영 compose 프로젝트 (`compose.prod.yaml`, 프로젝트 이름 `kmcbeauty`)

운영 서버: 인프라 저장소 server-mini2 가 관리하는 서버(공유 nginx · PostgreSQL 18 · Redis, 앱은 모두 일반 docker compose
— Swarm 미사용). 공개 주소 `https://api-beauty.dongri.kr`. 첫 배포 절차는 `docs/deployment.md` 10절.

| 서비스 = container_name | 메모리 상한 | 네트워크 | 비고 |
|--------|------|---------|------|
| `kmcbeauty-api` | 512m | `shared_network_prod` | expose `:8000`(호스트 포트 없음). 헬스체크는 이미지(curl /health) |
| `kmcbeauty-celery-worker` | 512m | `shared_network_prod` | `--concurrency=2`, 헬스체크 끔 |
| `kmcbeauty-celery-beat` | 256m | `shared_network_prod` | 단일 인스턴스(중복 디스패치 방지), 헬스체크 끔 |

- 네트워크는 서버 인프라의 외부 bridge `shared_network_prod` 하나뿐이다. 공유 nginx(고정 IP) · `postgresql` · `redis` 가
  같은 네트워크에 있어 컨테이너 이름으로 닿는다. 서비스 이름 = container_name 이라 `api` 같은 일반 별칭이 생기지 않는다.
- 외부 포트 노출 없음: 공유 nginx 가 `kmcbeauty-api:8000` 으로 프록시한다(resolver + 변수 proxy_pass, 원본 `deploy/nginx/api-beauty.conf`).
- 전용 Redis 없음: 공유 Redis DB 5(캐시 · 리프레시 토큰 · Celery 브로커/결과), DB 6(rate limit). 비밀번호 필수.
- 프록시 신뢰: `.env.prod` 의 `FORWARDED_ALLOW_IPS` = `shared_network_prod` 에서의 nginx IP(`deploy.sh` 가 `docker inspect` 값과 비교).
  uvicorn 이 X-Forwarded-For 로 실제 클라이언트 IP 를 복원한다(rate limit 키). `*` 는 금지.
- 기동: `up -d --wait`(api healthy · celery running). compose 는 바뀐 컨테이너를 정지 → 재생성하므로 배포 때 api 가
  잠깐 응답하지 않는다(롤링 · 자동 롤백 없음 — 롤백 명령은 deploy.sh 가 출력).
- `restart: unless-stopped`, 로그는 json-file 10m × 3 회전. 이미지: `${IMAGE_TAG:?…}` 필수(미지정 시 렌더 실패).

### 설정 로딩 스택

`app/core/config.py` 의 `Settings` 싱글톤 한 곳에서 모든 설정을 읽는다.

| 소스 | 우선순위 | 비고 |
|------|---------|------|
| 환경변수 (`os.environ`) | 1 (최고) | 로컬 개발·테스트·CI |
| `.env` / `.env.prod` | 2 | `env_file` fallback |
| 시크릿 파일 (`/run/secrets/`) | 3 (기본) | `secrets_dir="/run/secrets"`, 값의 앞뒤 공백 제거 |

- 로컬 개발: `.env` 파일 (compose 가 `REDIS_URL` 을 `redis://redis:6379/0` 으로 덮어씀)
- 운영: compose secrets(서버 `secrets/` 파일) 마운트 + `.env.prod` 비시크릿 값 (`.env.prod` 에 시크릿 키를 두면 시크릿 파일을 덮어쓰므로 `scripts/deploy.sh` 사전 검사가 차단)
- `.env.example`: 로컬 변수 문서화 템플릿
- `.env.prod.example`: 운영 변수 문서화 템플릿 (시크릿은 `secrets/` 파일 목록과 URL 형식만 주석으로)

### 시크릿 파일 (compose secrets)

서버 배포 디렉터리의 `secrets/`(0700, 배포 계정 소유)에 파일(0644, 끝 줄바꿈 없음)을 두고 compose 가 읽기 전용
bind mount 한다. 컨테이너는 시스템 uid(appuser)로 돌므로 0644 여야 읽힌다.

| 파일 | target (컨테이너 내 파일) | 서비스 |
|--------------|------------------------|------|
| `firebase_service_account.json` | `firebase_service_account` | api, celery_worker, celery_beat |
| `secret_key` | `SECRET_KEY` | api, celery_worker, celery_beat, migration(일회성 run) |
| `fernet_key` | `FERNET_KEY` | api, celery_worker, celery_beat, migration(일회성 run) |
| `database_url` | `DATABASE_URL` | api, celery_worker, celery_beat, migration(일회성 run) |
| `redis_url` | `REDIS_URL` | api, celery_worker, celery_beat |
| `rate_limit_storage_uri` | `RATE_LIMIT_STORAGE_URI` | api |

`target` = Settings 필드명이라 앱 코드는 주입 방식과 무관하다. 교체는 파일을 바꾸고 `up -d --force-recreate`(앱은 시작 시에만 읽는다). `scripts/deploy.sh` 의 `SECRET_FILES` 와 compose 파일 목록은 가드 테스트가 대조한다. Redis URL 은 공유 Redis 비밀번호가 들어가 시크릿이다. Firebase 키는 실제 키 전까지 `{}`(초기화만 실패, 푸시 불가). `FERNET_KEY` 로 암호화해 저장하는 데이터는 현재 없다(`encrypt_token`/`decrypt_token` 미사용).

### CI/CD 파이프라인

| 파일 | 트리거 | 역할 |
|------|--------|------|
| `.github/workflows/ci.yml` | PR / `workflow_call` | ruff check + format-check + pytest (Python 3.13, pip 캐시) |
| `.github/workflows/cd.yml` | main push | CI(재사용) 통과 → GHCR 빌드·푸시 → 이미지 import 스모크 → OpenSSH 로 서버 강제 명령 `deploy <sha>` → `scripts/deploy_entry.sh`(main 조상 확인 · 체크아웃) → `scripts/deploy.sh`(사전 검사 · pull · pg_dump · 마이그레이션 · compose up --wait · 상태 확인 · 스모크) |

- 이미지 레지스트리: `ghcr.io/antam2010/kmcbeauty-api:<sha>` (불변 SHA 태그)
- 배포 시 `IMAGE_TAG` 환경변수 치환으로 `:latest` 함정 회피
- 배포 직렬화: 워크플로 단위 concurrency `production-deploy` (진행 중 배포는 취소하지 않음) + 서버 flock 잠금
- SSH: 서드파티 액션 없이 OpenSSH, `StrictHostKeyChecking=yes` + 시크릿 `DEPLOY_SSH_KNOWN_HOSTS` 로 호스트 키 고정. CI 키는 서버에서 `restrict,command="…/deploy_entry.sh"` 강제 명령에만 묶임
- GitHub 시크릿: `DEPLOY_SSH_HOST` · `DEPLOY_SSH_PORT` · `DEPLOY_SSH_USER` · `DEPLOY_SSH_KEY` · `DEPLOY_SSH_KNOWN_HOSTS` (`scripts/setup_github_secrets.sh`)
- 서버의 GHCR pull: deploy 잡의 `GITHUB_TOKEN`(packages: read)을 stdin 으로 넘겨 임시 DOCKER_CONFIG 로 login → pull → logout
- 사전 검사(deploy.sh): docker compose 사용 가능, `shared_network_prod` 존재, nginx 가 그 네트워크에 붙어 있음, `.env.prod` 의 `APP_ENV=production` · 시크릿 키 정의 금지 · `FORWARDED_ALLOW_IPS` = nginx IP, `secrets/` 0700 · 파일 6개 0644, HEAD = 요청 sha · 추적 파일 깨끗, `compose config -q`
- 운영 DB 는 `alembic upgrade head` 만(downgrade 없음, `compose run --rm` 일회성 컨테이너), 매 배포 전 `pg_dump -Fc` 백업
- 승인 게이트 없음: GitHub environment `production` 은 이력용(필수 리뷰어 없음) — main 병합 = 배포

---

## 의존성 관리

- `requirements.txt` 단일 파일로 관리 (전이 의존성까지 버전 고정)
- lock 파일(poetry.lock, pip-tools generated) 없음 — 재현 가능한 빌드를 위해 도입 권장
- uv 도입 평가 완료 — 현재 pip 방식 유지 결정 (평가 기록: `docs/deployment.md`)
