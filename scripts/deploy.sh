#!/usr/bin/env bash
# ==============================================================================
# 운영 배포 스크립트 — 단일 서버 docker compose (SPEC-INFRA-001/002, 운영 서버 적용)
#
# 서버의 배포 디렉터리(DEPLOY_DIR, 기본 /srv/server-mini2/web/kmcbeauty-fastapi)에서 배포 계정으로
# 실행한다. CD 는 SSH 강제 명령(scripts/deploy_entry.sh)이 대상 커밋을 체크아웃한 뒤 이 스크립트를
# exec 한다. 수동 실행도 같다(대상 커밋을 먼저 체크아웃한다):
#   git checkout --detach <sha> && scripts/deploy.sh <sha>
#
# 순서: 잠금 → 전제 검사 → 이미지 pull → 직전 이미지 기록 → DB 백업(pg_dump) → 마이그레이션(upgrade 만)
#       → docker compose up -d --wait → 상태 확인 → 스모크 → 요약(롤백 명령 안내)
#   서버 상태를 바꾸는 단계(백업부터)는 전제 검사를 모두 통과해야 시작한다.
#   compose 는 바뀐 컨테이너를 멈추고 다시 만든다 — api 는 재생성 동안 잠깐(수 초) 응답하지 않는다.
#   같은 태그를 다시 배포하면 바뀐 것이 없어 컨테이너를 그대로 둔다(시크릿 교체는 docs 9절).
#   자동 롤백은 없다 — 실패하면 출력된 롤백 명령을 실행한다.
#
# 환경 변수(모두 선택):
#   REGISTRY_TOKEN   GHCR pull 토큰(CD 는 잡의 GITHUB_TOKEN). 있으면 임시 DOCKER_CONFIG 로
#                    login → pull → logout 한다(배포 계정의 기존 docker 로그인은 건드리지 않는다).
#   REGISTRY_USER    토큰 사용자(기본 antam2010)
#   SMOKE_URL        외부 스모크 URL(기본 https://api-beauty.dongri.kr/health, 빈 값이면 생략)
#   PG_CONTAINER     공유 PostgreSQL 컨테이너(기본 postgresql)
#   PG_DB            백업 대상 DB(기본 kmcbeauty)
#   NGINX_CONTAINER  공유 nginx 컨테이너(기본 nginx — FORWARDED_ALLOW_IPS 비교와 내부 스모크)
#
# 종료 코드: 0 성공 / 2 전제 실패 / 3 이미지 pull 실패 / 4 백업·마이그레이션 실패
#           / 5 기동 실패(up --wait · 상태 확인) / 6 스모크 실패
# 롤백: 직전 커밋으로 scripts/deploy.sh <이전-sha>, 또는 이미지만
#       IMAGE_TAG=<직전 이미지> docker compose -p kmcbeauty -f compose.prod.yaml up -d --wait
#       — 스키마는 되돌리지 않는다. 그래서 마이그레이션은 expand/contract(하위 호환)만 허용한다.
#
# 공개 저장소라 Actions 로그도 공개된다: IP · 서브넷 · 앱 로그(클라이언트 IP)는 출력하지 않는다.
# ==============================================================================
set -euo pipefail
# 정규식·문자 클래스를 로케일과 무관하게 ASCII 로 고정한다.
export LC_ALL=C

PROJECT=kmcbeauty
COMPOSE_FILE=compose.prod.yaml
NET=shared_network_prod
IMAGE_REPO=ghcr.io/antam2010/kmcbeauty-api
API=kmcbeauty-api
WORKERS=(kmcbeauty-celery-worker kmcbeauty-celery-beat)
SECRETS_DIR=secrets
# compose.prod.yaml 의 secrets.*.file 과 같아야 한다(tests/test_infra_guards.py 가 대조).
SECRET_FILES=(secret_key fernet_key database_url redis_url rate_limit_storage_uri firebase_service_account.json)
# .env.prod 에 정의하면 /run/secrets 값을 덮어쓰는 키(Settings 우선순위 env > .env > secrets).
SECRET_FIELDS='SECRET_KEY|FERNET_KEY|DATABASE_URL|REDIS_URL|RATE_LIMIT_STORAGE_URI'
ENV_FILE=.env.prod

REGISTRY_USER="${REGISTRY_USER:-antam2010}"
SMOKE_URL="${SMOKE_URL-https://api-beauty.dongri.kr/health}"
PG_CONTAINER="${PG_CONTAINER:-postgresql}"
PG_DB="${PG_DB:-kmcbeauty}"
PG_BACKUP_DIR=/var/lib/postgresql/backup
NGINX_CONTAINER="${NGINX_CONTAINER:-nginx}"

# 직전에 돌던 api 이미지(롤백 안내용) — 5단계에서 채운다.
PREV_IMAGE=""
PREV_SHA=""

say() { printf '%s %s\n' "$(date +%T)" "$*"; }
compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }
rollback_hint() {
  echo "롤백(배포 디렉터리에서 — 스키마는 되돌리지 않는다):"
  if [ -n "$PREV_SHA" ]; then
    echo "  git checkout --detach $PREV_SHA && scripts/deploy.sh $PREV_SHA"
  fi
  if [ -n "$PREV_IMAGE" ]; then
    echo "  IMAGE_TAG=$PREV_IMAGE docker compose -p $PROJECT -f $COMPOSE_FILE up -d --wait"
  else
    echo "  (직전 컨테이너가 없다 — 첫 배포. 내리려면 docker compose -p $PROJECT down)"
  fi
}
die() {
  local code="${2:-2}"
  say "✗ $1" >&2
  case "$code" in 5 | 6) rollback_hint >&2 ;; esac
  exit "$code"
}

# 레지스트리 토큰을 담은 임시 DOCKER_CONFIG 는 어떤 경로로 끝나도 지운다.
TMP_DOCKER_CONFIG=""
cleanup() { if [ -n "$TMP_DOCKER_CONFIG" ]; then rm -rf "$TMP_DOCKER_CONFIG"; fi; }
trap cleanup EXIT

# ── [0] 인자 · 입력 검증 · 잠금 ───────────────────────────────────────────
if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then
  die "사용법: scripts/deploy.sh <40자 소문자 커밋 sha>" 2
fi
SHA="$1"
IMAGE="${IMAGE_REPO}:${SHA}"
# 이름·URL 덮어쓰기는 명령 인자로만 쓰지만(eval 없음) 형식부터 확인한다.
for v in "$PG_CONTAINER" "$PG_DB" "$NGINX_CONTAINER"; do
  [[ "$v" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "컨테이너·DB 이름 형식 오류: $v" 2
done
case "$SMOKE_URL" in "" | https://* | http://*) ;; *) die "SMOKE_URL 은 http(s):// 로 시작하거나 비어야 한다" 2 ;; esac
[[ "$REGISTRY_USER" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] || die "REGISTRY_USER 형식 오류" 2

cd "$(dirname "$0")/.."

# 한 번에 하나만. deploy_entry.sh 가 잠금을 쥔 fd 9 를 넘겨주면(KMC_DEPLOY_LOCK_FD=9) 같은
# open file description 이라 flock 이 즉시 성공한다. 직접 실행하면 여기서 잠근다.
LOCK_FILE="$PWD/.git/kmcbeauty-deploy.lock"
if [ "${KMC_DEPLOY_LOCK_FD:-}" != 9 ]; then
  exec 9>>"$LOCK_FILE"
fi
flock -n 9 || die "다른 배포가 진행 중이다(잠금: $LOCK_FILE)" 2

# ── [1] 전제 검사(서버 상태를 바꾸기 전에 전부) ──────────────────────────
say "전제 검사 — $IMAGE"
docker compose version >/dev/null 2>&1 || die "docker compose(v2 플러그인)를 쓸 수 없다" 2
docker network inspect "$NET" >/dev/null 2>&1 \
  || die "$NET 네트워크가 없다 — 서버 인프라(server-mini2)가 만드는 외부 네트워크이며 이 스크립트는 만들지 않는다" 2
# nginx 의 shared_network_prod 주소. FORWARDED_ALLOW_IPS 비교에만 쓰고 출력하지 않는다.
nginx_ip="$(docker inspect "$NGINX_CONTAINER" \
  -f "{{with index .NetworkSettings.Networks \"$NET\"}}{{.IPAddress}}{{end}}" 2>/dev/null || true)"
[ -n "$nginx_ip" ] || die "$NGINX_CONTAINER 컨테이너가 없거나 $NET 에 붙어 있지 않다" 2

[ -f "$ENV_FILE" ] || die "$ENV_FILE 가 없다 — cp .env.prod.example .env.prod 후 값을 채운다(docs/deployment.md 10절 (d))" 2
# 정의 줄의 값(마지막 정의 기준). 앞 공백만 무시하고 값은 글자 그대로 본다(따옴표·주석도 값으로 본다).
env_value() {
  local line
  line="$(grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}
[ "$(env_value APP_ENV)" = production ] \
  || die "$ENV_FILE 의 APP_ENV 가 정확히 production 이 아니다(따옴표·공백 없이 APP_ENV=production)" 2
# 줄 번호만 출력한다(값은 로그에 남기지 않는다). 빈 값(KEY=)·값 없는 키(KEY)도 금지.
secret_lines="$(grep -nE "^[[:space:]]*(export[[:space:]]+)?(${SECRET_FIELDS})[[:space:]]*(=|$)" "$ENV_FILE" | cut -d: -f1 | paste -sd, - || true)"
[ -z "$secret_lines" ] \
  || die "$ENV_FILE ${secret_lines}번째 줄이 시크릿 파일 전용 키(${SECRET_FIELDS//|/ · })를 정의한다 — env 가 /run/secrets 보다 우선해 시크릿을 덮어쓰므로 지운다" 2
# 프록시 신뢰: 비면 모든 요청이 nginx IP 로 보여 rate limit 이 전 사용자 공용이 되고, '*' 면 위조 가능한
# X-Forwarded-For 를 믿는다. 기대값은 nginx 의 실제 주소(저장소에 적지 않고 여기서 읽는다).
fwd="$(env_value FORWARDED_ALLOW_IPS | tr -d '[:space:]')"
inspect_hint="docker inspect $NGINX_CONTAINER -f '{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}'"
[ -n "$fwd" ] || die "$ENV_FILE 의 FORWARDED_ALLOW_IPS 가 비어 있다 — 서버에서 $inspect_hint 값을 넣는다" 2
case "$fwd" in *'*'*) die "FORWARDED_ALLOW_IPS 에 '*' 가 있다 — X-Forwarded-For 위조로 rate limit 을 우회할 수 있다" 2 ;; esac
# 값 자체는 공개 로그에 남기지 않는다.
[ "$fwd" = "$nginx_ip" ] \
  || die "FORWARDED_ALLOW_IPS 가 $NET 의 $NGINX_CONTAINER 주소와 다르다 — 서버에서 $inspect_hint 값을 그대로 넣는다" 2

# 시크릿 파일: 디렉터리는 배포 계정만(0700), 파일은 컨테이너의 시스템 uid 가 읽도록 0644, 비어 있지 않음.
[ -d "$SECRETS_DIR" ] || die "$SECRETS_DIR/ 디렉터리가 없다(docs/deployment.md 10절 (c))" 2
[ "$(stat -c '%a' "$SECRETS_DIR")" = 700 ] || die "$SECRETS_DIR/ 권한이 700 이 아니다 — chmod 700 $SECRETS_DIR" 2
[ "$(stat -c '%u' "$SECRETS_DIR")" = "$(id -u)" ] || die "$SECRETS_DIR/ 소유자가 배포 계정이 아니다" 2
bad_secrets=()
for f in "${SECRET_FILES[@]}"; do
  p="$SECRETS_DIR/$f"
  if [ ! -f "$p" ] || [ ! -s "$p" ] || [ "$(stat -c '%a' "$p")" != 644 ]; then
    bad_secrets+=("$f")
  fi
done
[ "${#bad_secrets[@]}" -eq 0 ] \
  || die "시크릿 파일이 없거나 비었거나 0644 가 아니다: ${bad_secrets[*]} — docs/deployment.md 10절 (c)" 2

[ "$(git rev-parse HEAD)" = "$SHA" ] || die "HEAD 가 요청한 커밋이 아니다 — git checkout --detach $SHA 후 다시 실행" 2
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || die "추적 파일이 수정돼 있다(git status) — 커밋 그대로 배포하도록 되돌린다" 2

export IMAGE_TAG="$IMAGE"
compose config -q || die "$COMPOSE_FILE 렌더 실패(IMAGE_TAG · $ENV_FILE · 문법)" 2
say "✓ 전제 검사 통과"

# ── [2] 이미지 ──────────────────────────────────────────────────────────────
pull_image() {
  if [ -z "${REGISTRY_TOKEN:-}" ]; then
    docker pull --quiet "$IMAGE"
    return
  fi
  local rc=0
  TMP_DOCKER_CONFIG="$(mktemp -d)"
  if printf '%s\n' "$REGISTRY_TOKEN" \
    | DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login ghcr.io -u "$REGISTRY_USER" --password-stdin >/dev/null; then
    DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull --quiet "$IMAGE" || rc=$?
    DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker logout ghcr.io >/dev/null 2>&1 || true
  else
    rc=1
  fi
  rm -rf "$TMP_DOCKER_CONFIG"
  TMP_DOCKER_CONFIG=""
  return "$rc"
}
say "이미지 pull — $IMAGE"
if ! pull_image; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "이미지 pull 실패: $IMAGE (GHCR 권한·토큰 확인)" 3
  say "! pull 실패 — 이 서버에 이미 있는 같은 태그(불변 SHA)를 쓴다"
fi
unset REGISTRY_TOKEN

# ── [3] 직전 이미지 기록(롤백 안내용) ──────────────────────────────────────
PREV_IMAGE="$(docker inspect "$API" -f '{{.Config.Image}}' 2>/dev/null || true)"
case "$PREV_IMAGE" in
  "$IMAGE_REPO":*)
    prev_tag="${PREV_IMAGE##*:}"
    if [[ "$prev_tag" =~ ^[0-9a-f]{40}$ ]]; then PREV_SHA="$prev_tag"; fi
    ;;
esac
say "직전 이미지: ${PREV_IMAGE:-없음(첫 배포)}"

# ── [4] 백업 ────────────────────────────────────────────────────────────────
BACKUP_FILE="${PG_BACKUP_DIR}/${PG_DB}-$(date +%Y%m%d-%H%M%S).dump"
say "DB 백업(pg_dump -Fc) → ${PG_CONTAINER}:${BACKUP_FILE}"
docker exec "$PG_CONTAINER" pg_dump -U postgres -d "$PG_DB" -Fc -f "$BACKUP_FILE" \
  || die "pg_dump 실패 — DB·role 준비(docs/deployment.md 10절 (b))와 $PG_CONTAINER 상태를 확인한다. 서버는 바뀌지 않았다" 4
docker exec "$PG_CONTAINER" pg_restore -l "$BACKUP_FILE" >/dev/null \
  || die "백업 파일 검증(pg_restore -l) 실패: $BACKUP_FILE" 4

# ── [5] 마이그레이션(upgrade 만 — downgrade 없음) ─────────────────────────
# 새 이미지의 일회성 컨테이너(--rm, 의존 서비스 없이)로 실행한다. 시크릿·env·네트워크는 api 와 같다.
# 실패하면 컨테이너는 건드리지 않는다(PostgreSQL 은 DDL 이 트랜잭션이라 스키마도 그대로다).
say "마이그레이션(alembic upgrade head)…"
compose run --rm --no-deps -T "$API" alembic upgrade head \
  || die "마이그레이션 실패 — 컨테이너는 바꾸지 않았다(백업: ${PG_CONTAINER}:${BACKUP_FILE})" 4
current="$(compose run --rm --no-deps -T "$API" alembic current 2>&1)" \
  || { printf '%s\n' "$current" | tail -n 5; die "alembic current 실패" 4; }
case "$current" in
  *"(head)"*) say "✓ DB 가 head 다" ;;
  *)
    printf '%s\n' "$current" | tail -n 5
    die "alembic current 가 head 가 아니다" 4
    ;;
esac

# ── [6] 기동 ────────────────────────────────────────────────────────────────
say "docker compose up -d --wait…"
compose up -d --remove-orphans --wait --wait-timeout 180 \
  || die "compose up 실패(--wait 시간 초과 또는 비정상 종료) — docker compose -p $PROJECT ps 로 확인" 5
api_health="$(docker inspect "$API" -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
[ "$api_health" = healthy ] || die "$API 가 healthy 가 아니다(${api_health:-상태 없음})" 5
for c in "$API" "${WORKERS[@]}"; do
  state="$(docker inspect "$c" -f '{{.State.Status}}' 2>/dev/null || true)"
  [ "$state" = running ] || die "$c 가 실행 중이 아니다(${state:-없음})" 5
  img="$(docker inspect "$c" -f '{{.Config.Image}}' 2>/dev/null || true)"
  [ "$img" = "$IMAGE" ] || die "$c 의 이미지가 이번 배포 이미지가 아니다" 5
done
say "✓ $API healthy · ${WORKERS[*]} running"

# ── [7] 스모크 ──────────────────────────────────────────────────────────────
# 재시도: nginx resolver 가 직전의 "이름 없음" 응답을 valid=10s 동안 캐시할 수 있다.
retry() { # $1 = 횟수, 나머지 = 명령
  local n="$1" i
  shift
  for ((i = 1; i <= n; i++)); do
    if "$@"; then return 0; fi
    if [ "$i" -lt "$n" ]; then sleep 5; fi
  done
  return 1
}
say "스모크: ${NGINX_CONTAINER} → ${API}:8000/health ($NET)"
retry 6 docker exec "$NGINX_CONTAINER" wget -qO- -T 10 "http://${API}:8000/health" \
  || die "${NGINX_CONTAINER} → ${API} /health 실패(같은 네트워크·컨테이너 이름 확인)" 6
echo
if [ -n "$SMOKE_URL" ]; then
  say "스모크: 외부 $SMOKE_URL"
  retry 6 curl -fsS --max-time 10 "$SMOKE_URL" \
    || die "외부 /health 실패 — nginx 설정·Cloudflare DNS 확인(docs/deployment.md 10절 (g))" 6
  echo
else
  say "외부 스모크 생략(SMOKE_URL 빈 값)"
fi

# ── [8] 요약 ────────────────────────────────────────────────────────────────
docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '  {{.Names}}  {{.Image}}  {{.Status}}'
say "✓ 배포 완료 $IMAGE"
echo "백업: ${PG_CONTAINER}:${BACKUP_FILE}"
rollback_hint
