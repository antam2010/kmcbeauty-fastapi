#!/bin/bash
# ==============================================================================
# GitHub Actions 저장소 시크릿 등록/점검 스크립트
#   SPEC-INFRA-002 REQ-INFRA-110 / REQ-INFRA-111
#
# 목적:
#   - UI 수동 등록을 대체해 `gh secret set` 으로 CD 파이프라인 시크릿을 등록한다.
#   - 값은 저장소에 하드코딩하지 않는다. 환경변수 또는 안전한 프롬프트(no-echo)로 받는다.
#   - 대상 시크릿: SWARM_SSH_HOST, SWARM_SSH_USER, SWARM_SSH_KEY (필수)
#                  SWARM_SSH_FINGERPRINT (선택 — SSH 호스트 키 검증. 미등록 시 CD 가 검증 생략)
#     (GHCR 푸시는 GITHUB_TOKEN 자동 제공이라 등록 대상이 아니다.)
#
# 사용법:
#   # 1) 등록 상태 점검(값은 표시되지 않음). 필수 시크릿이 없으면 종료 코드 1:
#   ./scripts/setup_github_secrets.sh --check
#
#   # 2) 등록/갱신(idempotent — gh secret set 은 존재 시 덮어씀):
#   #    값은 환경변수로 주거나(비대화형), 미설정 시 대화형으로 입력받는다.
#   #    SWARM_SSH_FINGERPRINT 는 선택이며 주어졌을 때만 등록한다(대화형 입력 없음).
#   SWARM_SSH_HOST=1.2.3.4 \
#   SWARM_SSH_USER=deploy \
#   SWARM_SSH_KEY_FILE=~/.ssh/deploy_key \
#   SWARM_SSH_FINGERPRINT='SHA256:...' \
#     ./scripts/setup_github_secrets.sh
#
# 호스트 키 지문(SWARM_SSH_FINGERPRINT) 구하기 — 매니저 노드에서 직접 실행(네트워크 스캔 값은
# 중간자 공격을 걸러내지 못하므로 쓰지 않는다):
#   ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub | awk '{print $2}'
#   - 출력 형식 "SHA256:<43자 base64>" 를 접두사까지 그대로 등록한다.
#   - CD 의 drone-ssh(Go x/crypto)는 ECDSA > RSA > Ed25519 순으로 호스트 키를 고른다.
#     ECDSA 키가 없을 때만 ssh_host_rsa_key.pub, 그것도 없으면 ssh_host_ed25519_key.pub 를 쓴다.
#
# 전제:
#   - `gh` CLI 가 설치되어 있고 `gh auth login` 으로 대상 저장소 admin 권한이 인증돼 있다.
#   - `--repo` 를 주지 않으면 현재 디렉터리의 git remote 로 저장소를 자동 판별한다.
# ==============================================================================
set -euo pipefail

REPO_FLAG=""
MODE="set"
REQUIRED_SECRETS="SWARM_SSH_HOST SWARM_SSH_USER SWARM_SSH_KEY"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --repo) shift; REPO_FLAG="--repo $1" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
  esac
  shift
done

if ! command -v gh >/dev/null 2>&1; then
  echo "❌ gh CLI 가 필요합니다. https://cli.github.com 에서 설치 후 'gh auth login' 하세요." >&2
  exit 1
fi

# shellcheck disable=SC2086
if [ "$MODE" = "check" ]; then
  echo "🔎 현재 등록된 저장소 시크릿(값은 표시되지 않음):"
  SECRET_LIST="$(gh secret list $REPO_FLAG)"
  printf '%s\n' "$SECRET_LIST"
  echo
  REGISTERED="$(printf '%s\n' "$SECRET_LIST" | awk '{print $1}')"
  missing=0
  for name in $REQUIRED_SECRETS; do
    if printf '%s\n' "$REGISTERED" | grep -qx "$name"; then
      echo "✅ $name"
    else
      echo "❌ $name (필수) 미등록"
      missing=1
    fi
  done
  if printf '%s\n' "$REGISTERED" | grep -qx SWARM_SSH_FINGERPRINT; then
    echo "✅ SWARM_SSH_FINGERPRINT (선택) — CD 가 호스트 키를 검증합니다."
  else
    echo "WARN: SWARM_SSH_FINGERPRINT (선택) 미등록 — CD 가 호스트 키를 검증하지 않습니다."
  fi
  exit "$missing"
fi

# --- 값 수집(하드코딩 금지): 환경변수 우선, 없으면 대화형 입력 ---
if [ -z "${SWARM_SSH_HOST:-}" ]; then
  read -r -p "SWARM_SSH_HOST (매니저 노드 호스트/IP): " SWARM_SSH_HOST
fi
if [ -z "${SWARM_SSH_USER:-}" ]; then
  read -r -p "SWARM_SSH_USER (배포 계정): " SWARM_SSH_USER
fi

# SSH 개인키는 파일 경로(SWARM_SSH_KEY_FILE)로 받는 것을 권장(히스토리 노출 방지).
SSH_KEY_VALUE=""
if [ -n "${SWARM_SSH_KEY_FILE:-}" ]; then
  [ -f "$SWARM_SSH_KEY_FILE" ] || { echo "❌ SWARM_SSH_KEY_FILE 경로 없음: $SWARM_SSH_KEY_FILE" >&2; exit 1; }
  SSH_KEY_VALUE="$(cat "$SWARM_SSH_KEY_FILE")"
elif [ -n "${SWARM_SSH_KEY:-}" ]; then
  SSH_KEY_VALUE="$SWARM_SSH_KEY"
else
  echo "SWARM_SSH_KEY_FILE(권장) 또는 SWARM_SSH_KEY 환경변수로 개인키를 제공하세요." >&2
  exit 1
fi

if [ -z "$SWARM_SSH_HOST" ] || [ -z "$SWARM_SSH_USER" ] || [ -z "$SSH_KEY_VALUE" ]; then
  echo "❌ 필수 값이 비었습니다. 등록을 중단합니다." >&2
  exit 1
fi

# 지문은 등록 전에 형식부터 검증한다(drone-ssh 는 "SHA256:<패딩 없는 base64 43자>" 와 문자열 비교).
FINGERPRINT="${SWARM_SSH_FINGERPRINT:-}"
if [ -n "$FINGERPRINT" ] && ! [[ "$FINGERPRINT" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]]; then
  echo "❌ SWARM_SSH_FINGERPRINT 형식 오류: 'SHA256:' 접두사 + 43자(패딩 '=' 없음)여야 합니다." >&2
  echo "   매니저 노드에서: ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub | awk '{print \$2}'" >&2
  exit 1
fi

echo "🔐 시크릿 등록/갱신(gh secret set, idempotent)..."
# shellcheck disable=SC2086
printf '%s' "$SWARM_SSH_HOST" | gh secret set SWARM_SSH_HOST $REPO_FLAG
# shellcheck disable=SC2086
printf '%s' "$SWARM_SSH_USER" | gh secret set SWARM_SSH_USER $REPO_FLAG
# shellcheck disable=SC2086
printf '%s' "$SSH_KEY_VALUE" | gh secret set SWARM_SSH_KEY $REPO_FLAG
if [ -n "$FINGERPRINT" ]; then
  # shellcheck disable=SC2086
  printf '%s' "$FINGERPRINT" | gh secret set SWARM_SSH_FINGERPRINT $REPO_FLAG
else
  echo "ℹ️  SWARM_SSH_FINGERPRINT 미지정 — 등록하지 않음(기존 값 유지, 없으면 CD 가 호스트 키 검증 생략)."
fi

echo "✅ 완료. 확인: ./scripts/setup_github_secrets.sh --check"
