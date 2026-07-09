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

컨테이너 배포는 docker-compose 대신 단일 Swarm 스택(docker-stack.yml)을 사용합니다.
배포 상세(시크릿/마이그레이션/롤백/CI-CD)는 ``docs/deployment.md`` 를 참조하세요.

배포는 로컬 ``.env`` 가 아니라 ``.env.prod`` 를 사용합니다(REDIS_URL 을 오버레이
서비스 DNS ``redis://redis:6379/0`` 로 설정). 최초 1회 ``cp .env.prod.example .env.prod``
후 실제 값을 채웁니다. 이미지 태그는 가변 ``latest`` 가 아니라 불변 커밋 SHA(IMAGE_TAG)를 사용합니다.

Swarm 배포(단일 노드, 로컬 빌드 이미지)::

    ./scripts/start_swarm.sh
    # 또는 수동(불변 태그 주입):
    IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<git-sha> \
        docker stack deploy -c docker-stack.yml kmcbeauty


3. Alembic 마이그레이션
==========================

마이그레이션 파일 생성::

    alembic revision --autogenerate -m "update user model: rename password, add age"

DB에 마이그레이션 반영::

    alembic upgrade head

DB에 마이그레이션 롤백::

    alembic downgrade -1

4. FastAPI API 문서 접속
==========================

브라우저에서 아래 주소로 접속:

- Swagger UI: http://localhost:3200/docs
- ReDoc: http://localhost:3200/redoc

(로컬 uvicorn 기준. Swarm 배포 시 호스트 포트 미공개 — 외부 NGINX가 kmcbeauty_api:8000 라우팅)


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
    docker-stack.yml
    Dockerfile
    scripts/
    ├── start_local.sh
    ├── start_swarm.sh
    ├── deploy_migrate.sh
    ├── setup_github_secrets.sh
    └── stop.sh

7. 로컬 개발 워크플로우
========================

백킹 서비스(Redis, 선택 MySQL) 기동::

    # Redis 만
    docker compose -f docker/compose/compose.dev.yml up -d
    # Redis + MySQL(선택)
    docker compose -f docker/compose/compose.dev.yml --profile mysql up -d

pre-commit 훅 설치(커밋 시 ruff lint+format 자동 실행)::

    pip install pre-commit
    pre-commit install

테스트 실행(커버리지 리포트 포함)::

    pytest --cov=app --cov=worker --cov-report=term-missing

코드 스타일 수동 적용(ruff lint + format)::

    ruff check --fix . && ruff format .

8. 2노드 확장
========================

Swarm 워커 노드 조인, placement 제약(celery_beat/redis), NGINX 업스트림 고려사항은
``docs/scaling.md`` 를 참조하세요.

추가 TODO
=============

- seed 데이터 추가 방법 문서화
- ``Makefile``로 명령어 자동화 정리