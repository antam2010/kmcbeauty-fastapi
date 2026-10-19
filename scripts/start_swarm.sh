#!/bin/bash
# ==============================================================================
# 단일 노드 로컬 Swarm 배포 스크립트 (SPEC-INFRA-001)
# - 가변 `latest` 태그를 사용하지 않는다(REQ-INFRA-003).
# - 로컬 빌드 이미지를 불변 태그 규약(IMAGE_TAG)으로 주입해 배포한다.
# - 배포는 `.env.prod` 를 참조하므로 사전에 존재해야 한다(REQ-INFRA-001).
# ==============================================================================
set -e
cd "$(dirname "$0")/.."

APP_NAME="kmcbeauty"
# 로컬 단일 노드 전용 불변 태그. docker-stack.yml 의 기본값과 일치시킨다.
IMAGE_TAG="kmcbeauty-api:local"
export IMAGE_TAG

if [ ! -f .env.prod ]; then
  echo "❌ .env.prod 가 없습니다. .env.prod.example 을 복사해 값을 채우세요:"
  echo "   cp .env.prod.example .env.prod"
  exit 1
fi

echo "🚀 [PROD] FastAPI 앱 이미지 빌드 중... ($IMAGE_TAG)"
docker build -t "$IMAGE_TAG" .

echo "🚢 [PROD] Docker Swarm에 스택 배포 중... (docker-stack.yml)"
docker swarm init 2>/dev/null || true
docker network create --driver overlay --attachable shared_network_prod 2>/dev/null || true
docker stack deploy -c docker-stack.yml "$APP_NAME"

echo "✅ 배포 완료. 호스트 포트 미공개 — 외부 NGINX가 kmcbeauty_api:8000 으로 라우팅."
