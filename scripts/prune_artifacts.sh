#!/usr/bin/env bash
# ==============================================================================
# 배포 산출물 정리 — deploy.sh 가 배포에 성공한 뒤 호출한다(실패해도 배포는 성공으로 둔다).
#
#   scripts/prune_artifacts.sh <pg_container> <backup_dir> <db> <keep_dumps> \
#                              <image_repo> <current_image> [prev_image]
#
# 1) DB 덤프: 공유 PostgreSQL 컨테이너의 <backup_dir> 에서 이 DB 의 덤프
#    (<db>-YYYYMMDD-HHMMSS.dump, deploy.sh 4단계 형식)만 이름(=시각) 역순으로 세어
#    최신 <keep_dumps> 개를 남기고 지운다. 다른 앱 · 다른 형식의 파일은 건드리지 않는다.
#    지운 덤프는 복구할 수 없다.
#    덤프 이름의 시각 순서를 최신으로 본다 — 같은 형식의 수동 덤프는 서버 시각(deploy.sh 와 같은
#    시계)으로 만든다.
# 2) API 이미지: <image_repo>:<40자 sha> 태그 중 현재 · 직전(롤백용) 이미지만 남긴다.
#    직전 이미지가 없거나 현재와 같으면 지우지 않는다. 지워도 GHCR 에서 다시 받을 수 있다.
#    컨테이너가 쓰는 이미지는 docker rmi 가 거절한다.
#
# 종료 코드: 0 성공 / 1 일부 실패 / 2 인자 오류
# ==============================================================================
set -euo pipefail
export LC_ALL=C

say() { printf '%s %s\n' "$(date +%T)" "$*"; }

if [ "$#" -lt 6 ] || [ "$#" -gt 7 ]; then
  say "사용법: $0 <pg_container> <backup_dir> <db> <keep_dumps> <image_repo> <current_image> [prev_image]" >&2
  exit 2
fi
PG_CONTAINER="$1"
BACKUP_DIR="$2"
DB="$3"
KEEP_DUMPS="$4"
IMAGE_REPO="$5"
CURRENT_IMAGE="$6"
PREV_IMAGE="${7:-}"

# 이번 배포의 덤프를 지우지 않도록 최소 1개는 남긴다.
[[ "$KEEP_DUMPS" =~ ^[1-9][0-9]*$ ]] || { say "keep_dumps 는 1 이상의 정수여야 한다: $KEEP_DUMPS" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || { say "DB 이름 형식 오류: $DB" >&2; exit 2; }
[[ "$BACKUP_DIR" =~ ^/[A-Za-z0-9/_.-]+$ ]] || { say "backup_dir 형식 오류: $BACKUP_DIR" >&2; exit 2; }

rc=0

# ── 1) DB 덤프 ───────────────────────────────────────────────────────────────
# 컨테이너 안의 sh 에서 실행한다. 인자는 위치 인자로 넘겨 따옴표 해석을 피한다.
# shellcheck disable=SC2016
# 일반 파일만 센다(같은 이름의 디렉터리는 무시). 이름은 형식상 공백이 없어 단어 분리가 안전하다.
# 하나라도 못 지우면 1 로 끝나되 나머지는 계속 지운다.
prune_dumps_sh='
cd "$1" || exit 1
keep="$3"
old="$(for f in "$2"-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].dump; do
  [ -f "$f" ] && printf "%s\n" "$f"
done | sort -r | tail -n "+$((keep + 1))")"
rc=0
for f in $old; do
  if rm -f -- "$f"; then printf "%s\n" "$f"; else rc=1; fi
done
exit "$rc"
'
# 공유 PostgreSQL 컨테이너가 멈춰 있어도 배포가 끝나도록 시간 제한을 둔다.
if removed="$(timeout 60 docker exec "$PG_CONTAINER" sh -c "$prune_dumps_sh" sh "$BACKUP_DIR" "$DB" "$KEEP_DUMPS")"; then
  if [ -n "$removed" ]; then
    say "덤프 삭제($(printf '%s\n' "$removed" | wc -l | tr -d ' ')개, 최신 ${KEEP_DUMPS}개 유지):"
    printf '%s\n' "$removed" | sed 's/^/  /'
  else
    say "덤프: 지울 것 없음(최신 ${KEEP_DUMPS}개 유지)"
  fi
else
  [ -n "$removed" ] && printf '%s\n' "$removed" | sed 's/^/  삭제: /'
  say "! 덤프 정리 실패(일부 또는 전체, $PG_CONTAINER:$BACKUP_DIR)" >&2
  rc=1
fi

# ── 2) API 이미지 ────────────────────────────────────────────────────────────
# 직전 이미지를 모르거나 현재와 같으면(첫 배포 · 같은 sha 재배포) 롤백 대상이 무엇인지
# 알 수 없으므로 이미지는 지우지 않는다.
if [ -z "$PREV_IMAGE" ] || [ "$PREV_IMAGE" = "$CURRENT_IMAGE" ]; then
  say "이미지: 직전 이미지가 없거나 현재와 같아 정리하지 않음"
elif images="$(timeout 60 docker image ls "$IMAGE_REPO" --format '{{.Repository}}:{{.Tag}}')"; then
  removed_images=0
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    [ "$img" = "$CURRENT_IMAGE" ] && continue
    [ "$img" = "$PREV_IMAGE" ] && continue
    # 배포가 만든 불변 sha 태그만 다룬다(다른 태그 · <none> 은 그대로 둔다).
    case "$img" in "$IMAGE_REPO":*) ;; *) continue ;; esac
    [[ "${img##*:}" =~ ^[0-9a-f]{40}$ ]] || continue
    if timeout 60 docker rmi "$img" >/dev/null; then
      say "이미지 삭제: $img"
      removed_images=$((removed_images + 1))
    else
      say "! 이미지 삭제 실패(사용 중일 수 있다): $img" >&2
      rc=1
    fi
  done <<<"$images"
  say "이미지: ${removed_images}개 삭제(현재 · 직전 유지)"
else
  say "! 이미지 목록 조회 실패: $IMAGE_REPO" >&2
  rc=1
fi

exit "$rc"
