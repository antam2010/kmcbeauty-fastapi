#!/bin/bash

echo "🛑 FastAPI 중지 중..."

# 운영 compose 프로젝트(kmcbeauty, compose.prod.yaml)의 컨테이너를 내린다 — 운영 서버에서만 의미가 있다.
# 프로젝트 이름만으로 내리므로 IMAGE_TAG·compose 파일이 필요 없다. 외부 네트워크(shared_network_prod)·
# secrets/·DB·Redis 는 그대로 남는다. 다시 올리려면 scripts/deploy.sh <sha> (docs/deployment.md 7절).
# 로컬 개발 compose 스택(프로젝트 kmcbeauty-dev)은 이 스크립트가 아니라 아래 명령으로 종료한다:
#   docker compose -f docker/compose/compose.dev.yml down
if [ -n "$(docker ps -aq --filter label=com.docker.compose.project=kmcbeauty 2>/dev/null)" ]; then
    docker compose -p kmcbeauty down
    echo "✅ compose 프로젝트(kmcbeauty) 중지 완료."
else
    echo "ℹ️ compose 프로젝트(kmcbeauty) 컨테이너 없음 — 내릴 것이 없다."
fi
echo "ℹ️ 로컬 compose 스택 종료: docker compose -f docker/compose/compose.dev.yml down (호스트 직접 실행 uvicorn 은 Ctrl+C)"
