#!/bin/bash
# ==============================================================================
# 배포 전 Alembic 마이그레이션 실행 스크립트
#   SPEC-INFRA-001 REQ-INFRA-005 (배포 전 마이그레이션)
#   SPEC-INFRA-002 REQ-INFRA-109 (일회성 Swarm 서비스로 시크릿 주입)
#
# 목적:
#   - 스택 업데이트(docker stack deploy) '이전'에 `alembic upgrade head` 를 실행한다.
#   - 마이그레이션은 하위 호환(expand/contract)을 지켜 롤링 중 구/신 레플리카가
#     동일 스키마에서 동시에 동작하도록 한다(무중단 전제). 상세 규칙은
#     docs/deployment.md 의 "마이그레이션(expand/contract)" 절 참조.
#
# 시크릿 주입(REQ-INFRA-109):
#   - 컨테이너 단발 실행 방식(도커 CLI 의 1회성 컨테이너)은 Swarm secret 을 마운트하지
#     못하므로 배제한다. 파일 기반 env 주입도 사용하지 않는다(프로세스 env/히스토리로
#     자격 증명이 노출될 위험).
#   - 대신 `--restart-condition=none` 일회성 Swarm 서비스로 실행해 시크릿을
#     `/run/secrets/<필드명>` 으로 마운트한다. alembic/env.py 가 `app.core.config.settings`
#     를 import 하므로 DATABASE_URL 뿐 아니라 필수 필드(SECRET_KEY/FERNET_KEY)도
#     시크릿으로 마운트하고, 비시크릿(ALGORITHM/토큰 만료)은 `-e` 로 전달한다.
#   - 태스크 종료 코드를 확인해 성공(0)일 때만 스택 배포로 진행한다(fail-fast).
#   - 멀티 노드 대비: 매니저 노드에 고정(--constraint node.role==manager — CD 가 이미지를 pull 하고
#     pg_hba 에 등록된 노드)하고, --with-registry-auth 로 GHCR 자격 증명을 태스크에 전달한다.
#   - 네트워크는 shared_network_prod(외부 PostgreSQL 이그레스용)를 쓴다. 스택 전용 backend 망은
#     첫 stack deploy 때 생성되므로 그 이전에 도는 마이그레이션에서는 쓸 수 없다.
#
# 실행 방식(택1):
#   1) 매니저 노드에서 배포용 이미지로 일회성 Swarm 서비스(권장):
#        IMAGE_TAG=ghcr.io/antam2010/kmcbeauty-api:<sha> ./scripts/deploy_migrate.sh
#   2) 앱 코드가 배치된 호스트에서 직접 실행(로컬 단일 노드):
#        DATABASE_URL=postgresql+psycopg://... ./scripts/deploy_migrate.sh --local
#      (Docker secret 전환 후 .env.prod 에 DATABASE_URL 이 없으므로 운영자가 직접 export 한다.)
#
# 전제:
#   - 컨테이너 경로: 필수 Docker secret(kmc_database_url_v1/kmc_secret_key_v1/kmc_fernet_key_v1)이
#     사전 등록되어 있고, 오버레이 네트워크 `shared_network_prod` 가 존재한다.
#   - PostgreSQL 은 컨테이너 외부(관리형/호스트)에 있으며 매니저 노드에서 접근 가능하다.
# ==============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-container}"

# ---------------------------------------------------------------------------
# --local: 앱 호스트에서 직접 alembic 실행. Docker secret 전환 후 .env.prod 에는
# DATABASE_URL 이 없으므로 운영자가 환경변수로 반드시 제공해야 한다(계약 위반 위험 1).
# ---------------------------------------------------------------------------
if [ "$MODE" = "--local" ]; then
  : "${DATABASE_URL:?DATABASE_URL 환경변수를 설정하세요 (Docker secret 전환 후 .env.prod 에 없음). 예) export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db}"
  echo "🗃️  [MIGRATE] 로컬 alembic upgrade head 실행..."
  # ENV=prod 는 alembic/env.py 가 .env.prod 를 고르는 값, APP_ENV 는 앱 허용값(local|debug|stage|production).
  ENV="${ENV:-prod}" APP_ENV="${APP_ENV:-production}" alembic upgrade head
  echo "✅ [MIGRATE] 로컬 마이그레이션 완료."
  exit 0
fi

# ---------------------------------------------------------------------------
# container(기본): 일회성 Swarm 서비스로 시크릿을 마운트해 마이그레이션 실행.
# ---------------------------------------------------------------------------
: "${IMAGE_TAG:?IMAGE_TAG 를 설정하세요 (예: ghcr.io/antam2010/kmcbeauty-api:<sha>)}"

MIGRATE_SVC="kmc_migrate_${GITHUB_SHA:-$(date +%s)}"
MIGRATE_TIMEOUT="${MIGRATE_TIMEOUT:-300}"

# 종료·오류 시 항상 일회성 서비스를 정리한다(잔여 서비스 방지).
cleanup() {
  docker service rm "$MIGRATE_SVC" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "🗃️  [MIGRATE] 일회성 Swarm 서비스로 alembic upgrade head 실행... ($IMAGE_TAG / $MIGRATE_SVC)"

# --detach 로 생성만 하고, 아래에서 태스크 완료를 폴링한다.
# 시크릿: DATABASE_URL/SECRET_KEY/FERNET_KEY 는 /run/secrets/<필드명> 으로 마운트(target=필드명).
# 비시크릿: ALGORITHM/토큰 만료/ENV 는 -e 로 전달(settings 필수 필드 충족).
docker service create \
  --name "$MIGRATE_SVC" \
  --restart-condition=none \
  --with-registry-auth \
  --constraint node.role==manager \
  --network shared_network_prod \
  --secret source=kmc_database_url_v1,target=DATABASE_URL \
  --secret source=kmc_secret_key_v1,target=SECRET_KEY \
  --secret source=kmc_fernet_key_v1,target=FERNET_KEY \
  -e ENV=prod \
  -e APP_ENV=production \
  -e ALGORITHM=HS256 \
  -e ACCESS_TOKEN_EXPIRE_SECONDS=900 \
  -e REFRESH_TOKEN_EXPIRE_SECONDS=1209600 \
  --detach \
  "$IMAGE_TAG" \
  alembic upgrade head

# --- 태스크 완료 폴링(현재 상태 기준) ---
waited=0
task_state=""
while :; do
  # 가장 최근 태스크의 현재 상태(예: "Complete 3 seconds ago", "Failed 2 seconds ago").
  task_state="$(docker service ps "$MIGRATE_SVC" --format '{{.CurrentState}}' 2>/dev/null | head -n1)"
  case "$task_state" in
    Complete*) break ;;
    Failed*|Rejected*|Shutdown*|Orphaned*) break ;;
  esac
  if [ "$waited" -ge "$MIGRATE_TIMEOUT" ]; then
    echo "❌ [MIGRATE] 시간 초과(${MIGRATE_TIMEOUT}s). 마지막 상태: ${task_state:-unknown}"
    docker service logs "$MIGRATE_SVC" 2>&1 | tail -n 100 || true
    exit 1
  fi
  sleep 3
  waited=$((waited + 3))
done

# --- 태스크 로그 출력(성공/실패 무관하게 진단용) ---
echo "🗃️  [MIGRATE] 마이그레이션 로그:"
docker service logs "$MIGRATE_SVC" 2>&1 | tail -n 200 || true

# --- 태스크 종료 코드 확인(Complete 가 아니면 실패로 간주하고 중단) ---
task_id="$(docker service ps "$MIGRATE_SVC" -q --no-trunc 2>/dev/null | head -n1)"
exit_code=""
if [ -n "$task_id" ]; then
  exit_code="$(docker inspect --format '{{ .Status.ContainerStatus.ExitCode }}' "$task_id" 2>/dev/null || echo "")"
fi

case "$task_state" in
  Complete*)
    if [ -n "$exit_code" ] && [ "$exit_code" != "0" ]; then
      echo "❌ [MIGRATE] 태스크는 Complete 이나 종료 코드 $exit_code — 실패로 처리."
      exit 1
    fi
    echo "✅ [MIGRATE] 마이그레이션 성공(종료 코드 ${exit_code:-0}). 이제 스택 배포를 진행하세요."
    ;;
  *)
    echo "❌ [MIGRATE] 마이그레이션 실패(상태: ${task_state:-unknown}, 종료 코드: ${exit_code:-N/A}). 스택 배포를 중단합니다."
    exit 1
    ;;
esac
