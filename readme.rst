======================================
KMCBeauty FastAPI 프로젝트 실행 가이드
======================================

1. 의존성 설치
================

가상환경을 사용하는 경우::

    python3 -m venv venv
    source venv/bin/activate

필수 패키지 설치::

    pip install -r requirements.txt

가상환경 종료::

    deactivate


2. 개발 서버 / 배포
===========================

로컬 개발 서버 실행 (uvicorn, localhost:3200)::

    ./scripts/start_local.sh

위 스크립트는 호스트에서 uvicorn 을 직접 실행합니다. 기본 로컬 개발 경로는 Docker Compose
스택(7절)이며, 호스트 직접 실행은 호스트에서 DB(PostgreSQL)에 접근 가능해야 합니다.

운영은 서버(인프라 저장소 server-mini2 가 관리)의 docker compose 프로젝트 ``kmcbeauty``
(``compose.prod.yaml``)로 돌며, ``main`` 에 병합하면 GitHub Actions(CD)가 자동 배포합니다
(CI → GHCR 이미지 빌드·푸시 → SSH 강제 명령 ``scripts/deploy_entry.sh`` → ``scripts/deploy.sh``).
공개 주소는 https://api-beauty.dongri.kr 이고, nginx 설정 원본은 ``deploy/nginx/api-beauty.conf`` 입니다.
첫 배포 준비·시크릿·롤백 절차는 ``docs/deployment.md`` (10절 첫 배포 runbook)를 참조하세요.

운영 설정은 로컬 ``.env`` 가 아니라 서버 배포 디렉터리의 ``.env.prod`` (비시크릿만, 템플릿
``.env.prod.example``)와 ``secrets/`` 파일(SECRET_KEY · FERNET_KEY · DATABASE_URL · REDIS_URL ·
RATE_LIMIT_STORAGE_URI · Firebase 키 — compose secrets 로 마운트)로 주입합니다. DB·Redis 는 서버의
공유 PostgreSQL·Redis 를 씁니다. 이미지 태그는 가변 ``latest`` 가 아니라 불변 커밋 SHA(IMAGE_TAG)입니다.

서버에서 수동 배포(배포 디렉터리에서, 대상 커밋을 먼저 체크아웃)::

    git fetch --prune origin main && git checkout --detach <40자-sha>
    scripts/deploy.sh <40자-sha>

서버에서 운영 컨테이너 중지(외부 네트워크·secrets/·DB 는 남음)::

    ./scripts/stop.sh


3. Alembic 마이그레이션
==========================

로컬 compose 스택(7절)에서는 아래 명령을 컨테이너 안에서 실행합니다:
``docker compose -f docker/compose/compose.dev.yml exec api <command>``

마이그레이션 파일 생성::

    alembic revision --autogenerate -m "update user model: rename password, add age"

DB에 마이그레이션 반영::

    alembic upgrade head

DB에 마이그레이션 롤백(로컬 전용 — 운영 DB 에는 ``upgrade`` 만 실행, ``docs/deployment.md`` 5절)::

    alembic downgrade -1

4. FastAPI API 문서 접속
==========================

브라우저에서 아래 주소로 접속:

- Swagger UI: http://localhost:3200/docs
- ReDoc: http://localhost:3200/redoc

(로컬 uvicorn 기준. 운영은 호스트 포트 미공개 — 공유 NGINX가 kmcbeauty-api:8000 라우팅,
운영(APP_ENV=production)에서는 API 문서를 공개하지 않음)


5. 의존성 목록 저장 (선택)
=============================

패키지 설치 이후 현재 환경의 의존성을 저장::

    pip freeze > requirements.txt


6. 디렉토리 구조 예시
========================

::

    app/
    ├── main.py
    ├── database.py
    ├── model/
    │   ├── base.py
    │   ├── user.py
    │   └── phonebook.py
    ├── schema/
    ├── crud/
    alembic/
    ├── versions/
    compose.prod.yaml
    Dockerfile
    deploy/
    └── nginx/api-beauty.conf
    scripts/
    ├── start_local.sh
    ├── deploy.sh
    ├── deploy_entry.sh
    ├── setup_github_secrets.sh
    └── stop.sh

7. 로컬 개발 워크플로우
========================

로컬 스택(api + redis, 핫리로드) 기동::

    docker compose -f docker/compose/compose.dev.yml up -d --build
    # Celery 워커(선택)
    docker compose -f docker/compose/compose.dev.yml --profile worker up -d
    # 종료
    docker compose -f docker/compose/compose.dev.yml down

DB 는 compose 에 포함되지 않으며 Windows 호스트에서 실행하는 PostgreSQL 을 사용합니다.
최초 1회 데이터베이스 ``kmcbeauty`` 를 생성(Windows 측 psql/pgAdmin 에서
``CREATE DATABASE kmcbeauty;``)하고 ``.env`` 의 ``DATABASE_URL`` 을 설정합니다.
컨테이너는 ``host.docker.internal:5432`` 로 호스트 PostgreSQL 에 접근합니다
(비밀번호 특수문자는 URL 인코딩, ``.env.example`` 참조)::

    DATABASE_URL=postgresql+psycopg://user:password@host.docker.internal:5432/kmcbeauty

마이그레이션은 컨테이너 안에서 실행합니다(WSL 셸에서는 Windows 방화벽 때문에
호스트 PostgreSQL 에 접근할 수 없음)::

    docker compose -f docker/compose/compose.dev.yml exec api alembic upgrade head

API 문서(Swagger UI): http://localhost:3200/docs

pre-commit 훅 설치(커밋 시 ruff lint+format 자동 실행)::

    pip install pre-commit
    pre-commit install

테스트 실행(커버리지 리포트 포함)::

    pytest --cov=app --cov=worker --cov-report=term-missing

코드 스타일 수동 적용(ruff lint + format)::

    ruff check --fix . && ruff format .

추가 TODO
=============

- seed 데이터 추가 방법 문서화
- ``Makefile``로 명령어 자동화 정리