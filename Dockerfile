# syntax=docker/dockerfile:1

# ==============================================================================
# Production Dockerfile (Docker Swarm)
# - Single-stage, production-oriented image for `docker stack deploy`.
# - App code is COPIED into the image (Swarm deploys images, not bind mounts).
# - Runs as a non-root user and listens on internal port 8000 (uvicorn default).
#   호스트/외부 노출 포트는 배포 계층(compose ports, NGINX)에서 매핑한다.
# ==============================================================================
FROM python:3.13-slim

# 작업 디렉토리
WORKDIR /app

# 파이썬 런타임 환경 변수
#  - PYTHONUNBUFFERED: 로그를 버퍼링 없이 즉시 stdout 으로 내보낸다(컨테이너 로깅 필수).
#  - PYTHONDONTWRITEBYTECODE: .pyc 생성 방지(이미지 클린 유지).
#  - PIP_NO_CACHE_DIR / PIP_DISABLE_PIP_VERSION_CHECK: 빌드 캐시/노이즈 최소화.
#  - TZ: 컨테이너 기본 타임존을 서비스 기준(Asia/Seoul)으로 고정.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

# 시스템 패키지 설치
#  - build-essential / pkg-config: PostgreSQL 드라이버 psycopg v3 는 바이너리 휠
#    (psycopg-binary, libpq 동봉)로 설치하므로 시스템 libpq 빌드가 필요 없다.
#    cryptography 등 네이티브 휠 빌드가 필요한 경우를 대비해 빌드 도구만 포함한다.
#  - curl: HEALTHCHECK 에서 사용.
# 설치 후 apt 캐시를 제거해 이미지 크기를 줄인다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치(레이어 캐시 최적화: 소스 변경 시에도 의존성 레이어 재사용)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사(바인드 마운트 대신 이미지에 포함)
COPY . /app/

# 비루트 사용자 생성 및 소유권 이전(권한 최소화)
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --home-dir /app --no-create-home appuser \
    && chown -R appuser:appgroup /app
USER appuser

# 내부 서비스 포트(uvicorn 기본값 8000; 호스트로 직접 노출하지 않음)
EXPOSE 8000

# 헬스체크: 앱의 /health 엔드포인트(app/main.py) 확인
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 컨테이너 실행 커맨드(uvicorn, 포트 미지정 → 기본 8000)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
