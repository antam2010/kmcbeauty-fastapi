#!/bin/bash

echo "🛑 FastAPI 중지 중..."

# Swarm 스택 배포(start_swarm.sh) 사용 시 제거.
# 로컬 개발 compose 스택은 이 스크립트가 아니라 아래 명령으로 종료한다:
#   docker compose -f docker/compose/compose.dev.yml down
if docker info 2>/dev/null | grep -q "Swarm: active"; then
    docker stack rm kmcbeauty
    echo "✅ Swarm 스택(kmcbeauty) 제거 완료."
else
    echo "ℹ️ Swarm 비활성 — 제거할 스택 없음."
fi
echo "ℹ️ 로컬 compose 스택 종료: docker compose -f docker/compose/compose.dev.yml down (호스트 직접 실행 uvicorn 은 Ctrl+C)"
