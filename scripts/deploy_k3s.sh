#!/usr/bin/env bash
# ==============================================================================
# 운영 배포 스크립트 — k3s 대상 (deploy.sh 가 .env.prod 의 DEPLOY_TARGET=k3s 를 보고 exec 한다)
#
# 앱은 k3s 클러스터의 namespace kmcbeauty 에서 돈다. 매니페스트(Deployment · Service · Ingress ·
# ConfigMap)와 Secret 은 인프라 저장소 server-mini2 가 관리한다(k8s/apps/kmcbeauty). 이 스크립트는
# 이미지 태그만 바꾼다 — 매니페스트를 적용하거나 Secret 을 만들지 않는다.
#
# 순서: 잠금 → 전제 검사 → 이미지 존재 확인 → 직전 이미지 기록 → DB 백업(pg_dump) →
#       마이그레이션(일회성 Job, upgrade 만) → kubectl set image + rollout status(api · worker · beat)
#       → 스모크 → 정리(덤프 · 이미지 보존 개수) → 요약(롤백 명령 안내)
#   PostgreSQL 은 이 서버(배포 계정이 있는 호스트)의 공유 컨테이너다 — 백업은 compose 대상과 같다.
#   api 는 RollingUpdate(새 Pod 가 ready 된 뒤 옛 Pod 를 내린다), beat 는 Recreate(둘이 겹치지 않는다).
#   자동 롤백은 없다 — rollout 이 실패하면 옛 Pod 가 그대로 남고, 출력된 롤백 명령을 실행한다.
#
# 환경 변수(모두 선택, deploy.sh 와 같은 것은 뜻도 같다):
#   KUBECONFIG       namespace kmcbeauty 전용 계정(기본 ~/.kube/kmcbeauty-deployer.yaml)
#   REGISTRY_TOKEN   GHCR 토큰 — 이미지 존재 확인(docker manifest inspect)에만 쓴다. pull 은 kubelet 이 한다.
#   REGISTRY_USER    토큰 사용자(기본 antam2010)
#   SMOKE_URL        외부 스모크 URL(기본 https://api-beauty.dongri.kr/health, 빈 값이면 생략)
#   PG_CONTAINER · PG_DB · KEEP_DUMPS   deploy.sh 와 같다
#
# 종료 코드: deploy.sh 와 같다 — 0 성공 / 2 전제 실패 / 3 이미지 없음 / 4 백업·마이그레이션 실패
#           / 5 rollout 실패 / 6 스모크 실패
#
# 공개 저장소라 Actions 로그도 공개된다: IP · 서브넷 · 앱 로그는 출력하지 않는다(Job 로그도 마찬가지).
# ==============================================================================
set -euo pipefail
export LC_ALL=C

NS=kmcbeauty
IMAGE_REPO=ghcr.io/antam2010/kmcbeauty-api
# <Deployment>:<컨테이너> — server-mini2 k8s/apps/kmcbeauty/base 와 같아야 한다.
DEPLOYMENTS=(kmcbeauty-api:api kmcbeauty-celery-worker:worker kmcbeauty-celery-beat:beat)

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/kmcbeauty-deployer.yaml}"
REGISTRY_USER="${REGISTRY_USER:-antam2010}"
SMOKE_URL="${SMOKE_URL-https://api-beauty.dongri.kr/health}"
PG_CONTAINER="${PG_CONTAINER:-postgresql}"
PG_DB="${PG_DB:-kmcbeauty}"
PG_BACKUP_DIR=/var/lib/postgresql/backup
KEEP_DUMPS="${KEEP_DUMPS:-10}"

PREV_IMAGE=""
PREV_SHA=""

say() { printf '%s %s\n' "$(date +%T)" "$*"; }
kube() { kubectl -n "$NS" --request-timeout=30s "$@"; }
rollback_hint() {
  echo "롤백(스키마는 되돌리지 않는다):"
  if [ -n "$PREV_SHA" ]; then
    echo "  git checkout --detach $PREV_SHA && scripts/deploy.sh $PREV_SHA"
  fi
  local d
  for d in "${DEPLOYMENTS[@]}"; do
    echo "  kubectl -n $NS rollout undo deployment/${d%%:*}"
  done
}
die() {
  local code="${2:-2}"
  say "✗ $1" >&2
  case "$code" in 5 | 6) rollback_hint >&2 ;; esac
  exit "$code"
}

TMP_DOCKER_CONFIG=""
cleanup() { if [ -n "$TMP_DOCKER_CONFIG" ]; then rm -rf "$TMP_DOCKER_CONFIG"; fi; }
trap cleanup EXIT

# ── [0] 인자 · 입력 검증 · 잠금 ───────────────────────────────────────────
if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then
  die "사용법: scripts/deploy_k3s.sh <40자 소문자 커밋 sha>" 2
fi
SHA="$1"
IMAGE="${IMAGE_REPO}:${SHA}"
for v in "$PG_CONTAINER" "$PG_DB"; do
  [[ "$v" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "컨테이너·DB 이름 형식 오류: $v" 2
done
case "$SMOKE_URL" in "" | https://* | http://*) ;; *) die "SMOKE_URL 은 http(s):// 로 시작하거나 비어야 한다" 2 ;; esac
[[ "$REGISTRY_USER" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] || die "REGISTRY_USER 형식 오류" 2
[[ "$KEEP_DUMPS" =~ ^[1-9][0-9]*$ ]] || die "KEEP_DUMPS 는 1 이상의 정수여야 한다" 2

cd "$(dirname "$0")/.."

# deploy.sh 가 잠금 fd 9 를 넘겨준다(KMC_DEPLOY_LOCK_FD=9). 직접 실행하면 여기서 잠근다.
LOCK_FILE="$PWD/.git/kmcbeauty-deploy.lock"
if [ "${KMC_DEPLOY_LOCK_FD:-}" != 9 ]; then
  exec 9>>"$LOCK_FILE"
fi
flock -n 9 || die "다른 배포가 진행 중이다(잠금: $LOCK_FILE)" 2

# ── [1] 전제 검사(클러스터 · 서버 상태를 바꾸기 전에 전부) ─────────────────
say "전제 검사(k3s) — $IMAGE"
command -v kubectl >/dev/null 2>&1 || die "kubectl 이 없다" 2
[ -r "$KUBECONFIG" ] || die "kubeconfig 를 읽을 수 없다: $KUBECONFIG (server-mini2 docs/k8s/PLAN.md 11절)" 2
for d in "${DEPLOYMENTS[@]}"; do
  kube get deployment "${d%%:*}" >/dev/null 2>&1 \
    || die "deployment/${d%%:*} 이 없거나 볼 수 없다 — 매니페스트(server-mini2 k8s/apps/kmcbeauty)와 계정 권한 확인" 2
done
kube auth can-i create jobs >/dev/null 2>&1 || die "이 계정은 namespace $NS 에 Job 을 만들 수 없다" 2
docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || die "$PG_CONTAINER 컨테이너가 없다(백업 불가)" 2

[ "$(git rev-parse HEAD)" = "$SHA" ] || die "HEAD 가 요청한 커밋이 아니다 — git checkout --detach $SHA 후 다시 실행" 2
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || die "추적 파일이 수정돼 있다(git status) — 커밋 그대로 배포하도록 되돌린다" 2
say "✓ 전제 검사 통과"

# ── [2] 이미지 존재 확인(pull 은 클러스터의 kubelet 이 한다) ───────────────
manifest_ok() {
  if [ -z "${REGISTRY_TOKEN:-}" ]; then
    docker manifest inspect "$IMAGE" >/dev/null 2>&1
    return
  fi
  local rc=0
  TMP_DOCKER_CONFIG="$(mktemp -d)"
  if printf '%s\n' "$REGISTRY_TOKEN" \
    | DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login ghcr.io -u "$REGISTRY_USER" --password-stdin >/dev/null 2>&1; then
    DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker manifest inspect "$IMAGE" >/dev/null 2>&1 || rc=$?
  else
    rc=1
  fi
  rm -rf "$TMP_DOCKER_CONFIG"
  TMP_DOCKER_CONFIG=""
  return "$rc"
}
say "이미지 확인 — $IMAGE"
manifest_ok || die "레지스트리에 이미지가 없거나 읽을 수 없다: $IMAGE (GHCR 권한·토큰 확인)" 3
unset REGISTRY_TOKEN

# ── [3] 직전 이미지 기록(롤백 안내용) ──────────────────────────────────────
PREV_IMAGE="$(kube get deployment kmcbeauty-api -o 'jsonpath={.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
case "$PREV_IMAGE" in
  "$IMAGE_REPO":*)
    prev_tag="${PREV_IMAGE##*:}"
    if [[ "$prev_tag" =~ ^[0-9a-f]{40}$ ]]; then PREV_SHA="$prev_tag"; fi
    ;;
esac
say "직전 이미지: ${PREV_IMAGE:-없음}"

# ── [4] 백업 ────────────────────────────────────────────────────────────────
BACKUP_FILE="${PG_BACKUP_DIR}/${PG_DB}-$(date +%Y%m%d-%H%M%S).dump"
say "DB 백업(pg_dump -Fc) → ${PG_CONTAINER}:${BACKUP_FILE}"
docker exec "$PG_CONTAINER" pg_dump -U postgres -d "$PG_DB" -Fc -f "$BACKUP_FILE" \
  || die "pg_dump 실패 — $PG_CONTAINER 상태를 확인한다. 클러스터는 바뀌지 않았다" 4
docker exec "$PG_CONTAINER" pg_restore -l "$BACKUP_FILE" >/dev/null \
  || die "백업 파일 검증(pg_restore -l) 실패: $BACKUP_FILE" 4

# ── [5] 마이그레이션(일회성 Job — upgrade 만) ─────────────────────────────
# 새 이미지로 api 와 같은 ConfigMap · Secret 을 받아 돈다. 실패하면 Deployment 는 건드리지 않는다.
# Job 로그에는 alembic 의 리비전 줄만 남기고 그 밖(경고 · 접속 정보)은 출력하지 않는다.
JOB="migrate-$(date -u +%Y%m%d%H%M%S)"
say "마이그레이션(alembic upgrade head) — job/$JOB"
kube create -f - >/dev/null <<EOF || die "마이그레이션 Job 생성 실패" 4
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 300
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets: [{ name: ghcr-pull }]
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, runAsUser: 999, runAsGroup: 999 }
      containers:
        - name: migrate
          image: $IMAGE
          command:
            - sh
            - -c
            - |
              set -e
              alembic upgrade head > /dev/null 2>&1 || { echo "upgrade-failed"; exit 1; }
              echo "current=\$(alembic current 2> /dev/null | tail -n 1)"
          envFrom: [{ configMapRef: { name: kmcbeauty-env } }]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: [ALL] }
          volumeMounts:
            - { name: secrets, mountPath: /run/secrets, readOnly: true }
            - { name: tmp, mountPath: /tmp }
      volumes:
        - name: secrets
          secret: { secretName: kmcbeauty-secrets }
        - name: tmp
          emptyDir: { medium: Memory, sizeLimit: 64Mi }
EOF
state=""
for _ in $(seq 1 150); do
  state="$(kube get job "$JOB" -o 'jsonpath={.status.succeeded}/{.status.failed}' 2>/dev/null || true)"
  case "$state" in 1/*) break ;; */[1-9]*) break ;; esac
  sleep 2
done
logs="$(kube logs "job/$JOB" 2>/dev/null || true)"
case "$state" in
  1/*) ;;
  *) die "마이그레이션 실패(job/$JOB, 상태 ${state:-없음}) — Deployment 는 바꾸지 않았다(백업: ${PG_CONTAINER}:${BACKUP_FILE})" 4 ;;
esac
case "$logs" in
  *"(head)"*) say "✓ DB 가 head 다" ;;
  *) die "alembic current 가 head 가 아니다(job/$JOB)" 4 ;;
esac

# ── [6] 교체 ────────────────────────────────────────────────────────────────
for d in "${DEPLOYMENTS[@]}"; do
  name="${d%%:*}"
  container="${d##*:}"
  say "교체: deployment/$name"
  kube set image "deployment/$name" "$container=$IMAGE" >/dev/null \
    || die "deployment/$name 이미지 변경 실패" 5
  kubectl -n "$NS" rollout status "deployment/$name" --timeout=180s >/dev/null \
    || die "deployment/$name rollout 실패(시간 초과 또는 비정상 종료) — kubectl -n $NS get pods 로 확인" 5
done
say "✓ ${DEPLOYMENTS[*]%%:*} rollout 완료"

# ── [7] 스모크 ──────────────────────────────────────────────────────────────
retry() { # $1 = 횟수, 나머지 = 명령
  local n="$1" i
  shift
  for ((i = 1; i <= n; i++)); do
    if "$@"; then return 0; fi
    if [ "$i" -lt "$n" ]; then sleep 5; fi
  done
  return 1
}
if [ -n "$SMOKE_URL" ]; then
  say "스모크: 외부 $SMOKE_URL"
  retry 6 curl -fsS --max-time 10 "$SMOKE_URL" \
    || die "외부 /health 실패 — nginx · Traefik · Ingress 확인" 6
  echo
else
  say "외부 스모크 생략(SMOKE_URL 빈 값)"
fi

# ── [8] 정리(실패해도 배포는 성공으로 둔다) ────────────────────────────────
# 덤프는 이 DB 의 것만 최신 KEEP_DUMPS 개. 이 서버에 남은 compose 시절 이미지도 현재 · 직전만 남긴다.
say "정리: 덤프 최신 ${KEEP_DUMPS}개"
if ! bash scripts/prune_artifacts.sh "$PG_CONTAINER" "$PG_BACKUP_DIR" "$PG_DB" "$KEEP_DUMPS" \
  "$IMAGE_REPO" "$IMAGE" "$PREV_IMAGE"; then
  say "! 정리 일부 실패 — 배포는 완료됐다. 위 메시지를 보고 수동으로 정리한다"
fi

# ── [9] 요약 ────────────────────────────────────────────────────────────────
kube get pods -o 'custom-columns=POD:.metadata.name,STATUS:.status.phase,IMAGE:.spec.containers[0].image' 2>/dev/null \
  | sed 's/^/  /' || true
say "✓ 배포 완료(k3s) $IMAGE"
echo "백업: ${PG_CONTAINER}:${BACKUP_FILE}"
rollback_hint
