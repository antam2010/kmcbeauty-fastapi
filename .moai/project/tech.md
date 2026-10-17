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

- bearer 인증은 액세스 토큰만 허용 (`type == "access"` 검증), 소프트삭제(탈퇴) 유저는 즉시 401
- 샵 데이터 접근은 멤버십(`shop_user`) 기준이며, 예약의 고객·담당자·메뉴 상세 참조는 현재 샵 소속만 허용

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
| slowapi | 0.1.10 | 요청 속도 제한 — 저장소 `RATE_LIMIT_STORAGE_URI` (기본 `memory://`, 운영은 Redis DB 1) |
| limits | 5.8.0 | slowapi 저장소 백엔드 |
| fastapi-pagination | 0.15.15 | 페이지 기반 페이지네이션 |
| firebase-admin | 7.5 | FCM 푸시 알림 발송 (같은 샵 멤버 대상만 허용) |
| Sentry SDK | 2.64 | 에러 추적 및 성능 트레이싱 |

Sentry 설정: `traces_sample_rate=0.2`, `send_default_pii=False`(요청 쿠키·본문 자동 첨부 차단), 사용자 식별은 id 만 전송, 로컬·디버그 환경에서 비활성화.

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

### Docker Swarm 스택 (`docker-stack.yml`)

| 서비스 | 레플리카 | 포트 | 네트워크 | 비고 |
|--------|---------|------|---------|------|
| `kmcbeauty_api` | 2 | `:8000` (내부) | `shared_network_prod` + `backend` | rolling update(start-first) + 헬스 게이팅 + 자동 롤백 |
| `redis` | 1 | (내부) | `backend` | redis:7-alpine, appendonly, `redis_data` 볼륨. 접속명 `kmcbeauty_redis` (DB 0 브로커·캐시, DB 1 rate limit) |
| `celery_worker` | 2 | — | `backend` | 태스크 처리 |
| `celery_beat` | 1 | — | `backend` | 단일 레플리카 (중복 디스패치 방지) |

- 외부 포트 노출 없음: NGINX 가 `shared_network_prod` overlay 로 `kmcbeauty_api:8000` 에만 접근한다.
  Redis(무인증)·worker·beat 는 스택 전용 overlay `backend` 에만 연결된다.
- 프록시 신뢰: `.env.prod` 의 `FORWARDED_ALLOW_IPS` = `shared_network_prod` 서브넷 CIDR. uvicorn 이 X-Forwarded-For 로
  실제 클라이언트 IP 를 복원한다(rate limit 키). `*` 는 금지 — XFF 맨 왼쪽(위조 가능) 값을 쓰게 된다.
- 헬스체크: `curl /health` (Dockerfile 설정)
- 실서버는 아직 없음 — 첫 배포 절차는 `docs/deployment.md` 10절

### 설정 로딩 스택

`app/core/config.py` 의 `Settings` 싱글톤 한 곳에서 모든 설정을 읽는다.

| 소스 | 우선순위 | 비고 |
|------|---------|------|
| 환경변수 (`os.environ`) | 1 (최고) | 로컬 개발·테스트·CI |
| `.env` / `.env.prod` | 2 | `env_file` fallback |
| Docker secrets (`/run/secrets/`) | 3 (기본) | `secrets_dir="/run/secrets"` |

- 로컬 개발: `.env` 파일 (compose 가 `REDIS_URL` 을 `redis://redis:6379/0` 으로 덮어씀)
- Swarm 배포: Docker secrets 마운트 + `.env.prod` 비시크릿 값 (`.env.prod` 에 시크릿 키를 두면 secret 을 덮어쓰므로 CD 사전 검증이 차단)
- `.env.example`: 로컬 변수 문서화 템플릿
- `.env.prod.example`: 운영 변수 문서화 템플릿 (민감 키는 "managed via Docker secret" 표기)

### Docker secrets (Swarm)

| external 이름 | target (컨테이너 내 파일) | 서비스 |
|--------------|------------------------|------|
| `firebase_service_account` | `firebase_service_account` | api, celery_worker, celery_beat |
| `kmc_secret_key_v1` | `SECRET_KEY` | api, celery_worker, celery_beat |
| `kmc_fernet_key_v1` | `FERNET_KEY` | api, celery_worker, celery_beat |
| `kmc_database_url_v1` | `DATABASE_URL` | api, celery_worker, celery_beat, migration(일회성) |

`target` 은 불변 — 회전 시 external 이름(버전 suffix)만 교체하므로 앱 코드 변경 불필요. 첫 배포에서는 `kmc_database_url_v1` 에 PostgreSQL URL 을 넣는다.

### CI/CD 파이프라인

| 파일 | 트리거 | 역할 |
|------|--------|------|
| `.github/workflows/ci.yml` | PR / `workflow_call` | ruff check + format-check + pytest (Python 3.13, pip 캐시) |
| `.github/workflows/cd.yml` | main push | CI(재사용) 통과 → GHCR 빌드·푸시 → 이미지 import 스모크 → SSH 사전 검증 → 커밋 SHA 체크아웃 → 마이그레이션 → stack deploy |

- 이미지 레지스트리: `ghcr.io/antam2010/kmcbeauty-api:<sha>` (불변 SHA 태그)
- 배포 시 `IMAGE_TAG` 환경변수 치환으로 `:latest` 함정 회피
- 배포 직렬화: 워크플로 단위 concurrency `production-deploy` (진행 중 배포는 취소하지 않음)
- 사전 검증: 필수 Docker secret, `.env.prod` 의 시크릿 키 정의 금지, `REDIS_URL`(localhost 금지)·`FORWARDED_ALLOW_IPS`(`*` 금지) 필수, `shared_network_prod` 가 `overlay swarm`
- SSH 호스트 키 검증: 선택 시크릿 `SWARM_SSH_FINGERPRINT` (미등록 시 검증 생략)
- `--with-registry-auth` 로 멀티 노드에서도 GHCR pull
- Docker secrets: `firebase_service_account`, `kmc_secret_key_v1`, `kmc_fernet_key_v1`, `kmc_database_url_v1` (api, celery_worker, celery_beat 모두 마운트)
- CD 수동 승인 게이트: GitHub environment `production` 보호 규칙

---

## 의존성 관리

- `requirements.txt` 단일 파일로 관리 (전이 의존성까지 버전 고정)
- lock 파일(poetry.lock, pip-tools generated) 없음 — 재현 가능한 빌드를 위해 도입 권장
- uv 도입 평가 완료 — 현재 pip 방식 유지 결정 (평가 기록: `docs/deployment.md`)
