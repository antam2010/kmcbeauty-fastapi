#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "🚀 [DEV] FastAPI 로컬 개발 서버 시작 중... (uvicorn :3200)"
echo "ℹ️ 기본 로컬 개발 경로는 compose: docker compose -f docker/compose/compose.dev.yml up -d"
echo "ℹ️ 이 스크립트는 호스트에서 uvicorn 을 직접 실행 — 호스트에서 DB(DATABASE_URL)에 접근 가능해야 함."
echo "ℹ️ REDIS_URL 미설정 시 기본값 redis://redis:6379/0 — 로컬 redis 필요."

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-3200}" --reload
