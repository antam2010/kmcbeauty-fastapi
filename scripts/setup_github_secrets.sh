#!/bin/bash
# ==============================================================================
# GitHub Actions 저장소 시크릿 등록/점검 스크립트
#   SPEC-INFRA-002 REQ-INFRA-110 / REQ-INFRA-111
#
# 목적:
#   - UI 수동 등록을 대체해 `gh secret set` 으로 CD 배포 잡(cd.yml deploy)의 시크릿을 등록한다.
#   - 값은 저장소에 하드코딩하지 않는다(공개 저장소). 환경변수·파일 경로·대화형 입력으로만 받는다.
#   - 대상 시크릿(모두 필수):
#       DEPLOY_SSH_HOST        서버 SSH 호스트 — Cloudflare 프록시(주황 구름)를 거치지 않는 이름/IP
#       DEPLOY_SSH_PORT        서버 SSH 포트
#       DEPLOY_SSH_USER        배포 계정(docker 그룹, 배포 디렉터리 소유)
#       DEPLOY_SSH_KEY         CI 전용 개인키(암호 없음) — DEPLOY_SSH_KEY_FILE 경로로 준다.
#                              공개키는 서버에서 강제 명령(scripts/deploy_entry.sh)에 묶는다.
#       DEPLOY_SSH_KNOWN_HOSTS 서버 호스트 키(known_hosts 형식) — DEPLOY_SSH_KNOWN_HOSTS_FILE 경로로 준다.
#     (GHCR 푸시와 서버의 pull 은 잡마다 발급되는 GITHUB_TOKEN 을 쓰므로 등록 대상이 아니다.)
#
# 사용법:
#   # 1) 등록 상태 점검(값은 표시되지 않음). 필수 시크릿이 없으면 종료 코드 1:
#   ./scripts/setup_github_secrets.sh --check
#
#   # 2) 등록/갱신(idempotent — gh secret set 은 존재 시 덮어씀):
#   #    값은 환경변수로 주거나(비대화형), 미설정 시 대화형으로 입력받는다.
#   DEPLOY_SSH_HOST=<ssh-host> DEPLOY_SSH_PORT=<ssh-port> DEPLOY_SSH_USER=<deploy-user> \
#   DEPLOY_SSH_KEY_FILE=~/.ssh/kmcbeauty_cd \
#   DEPLOY_SSH_KNOWN_HOSTS_FILE=~/.ssh/kmcbeauty_known_hosts \
#     ./scripts/setup_github_secrets.sh
#
# known_hosts 파일 만들기(docs/deployment.md 10절 (f)) — 네트워크로 받은 키는 중간자 공격을 걸러내지
# 못하므로, 서버에서 직접 본 지문과 같을 때만 등록한다. 호스트 이름이 들어 있으므로 저장소 밖에 둔다:
#   ssh-keyscan -p <ssh-port> <ssh-host> > ~/.ssh/kmcbeauty_known_hosts
#   ssh-keygen -lf ~/.ssh/kmcbeauty_known_hosts                           # 운영자 PC
#   for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done    # 서버(신뢰된 세션)
#   이 스크립트도 등록 직전에 파일의 지문을 출력한다.
#
# 전제:
#   - `gh` CLI 가 설치되어 있고 `gh auth login` 으로 대상 저장소 admin 권한이 인증돼 있다.
#   - `ssh-keygen`(OpenSSH)이 있다(키·known_hosts 형식 검증에 쓴다).
#   - `--repo` 를 주지 않으면 현재 디렉터리의 git remote 로 저장소를 자동 판별한다.
# ==============================================================================
set -euo pipefail

REPO_FLAG=""
MODE="set"
REQUIRED_SECRETS="DEPLOY_SSH_HOST DEPLOY_SSH_PORT DEPLOY_SSH_USER DEPLOY_SSH_KEY DEPLOY_SSH_KNOWN_HOSTS"

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
  exit "$missing"
fi

fail() { echo "❌ $*" >&2; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || fail "ssh-keygen(OpenSSH)이 필요합니다."

# --- 값 수집(하드코딩 금지): 환경변수 우선, 없으면 대화형 입력 ---
if [ -z "${DEPLOY_SSH_HOST:-}" ]; then
  read -r -p "DEPLOY_SSH_HOST (Cloudflare 프록시가 아닌 서버 SSH 호스트/IP): " DEPLOY_SSH_HOST
fi
if [ -z "${DEPLOY_SSH_PORT:-}" ]; then
  read -r -p "DEPLOY_SSH_PORT (서버 SSH 포트): " DEPLOY_SSH_PORT
fi
if [ -z "${DEPLOY_SSH_USER:-}" ]; then
  read -r -p "DEPLOY_SSH_USER (배포 계정): " DEPLOY_SSH_USER
fi
# 개인키·known_hosts 는 값이 아니라 파일 경로로 받는다(히스토리·화면 노출 방지).
if [ -z "${DEPLOY_SSH_KEY_FILE:-}" ]; then
  read -r -p "DEPLOY_SSH_KEY_FILE (CI 전용 개인키 파일 경로): " DEPLOY_SSH_KEY_FILE
fi
if [ -z "${DEPLOY_SSH_KNOWN_HOSTS_FILE:-}" ]; then
  read -r -p "DEPLOY_SSH_KNOWN_HOSTS_FILE (known_hosts 파일 경로): " DEPLOY_SSH_KNOWN_HOSTS_FILE
fi

# --- 등록 전 검증(형식이 틀린 값으로 CD 가 실패하지 않게) ---
[ -n "$DEPLOY_SSH_HOST" ] || fail "DEPLOY_SSH_HOST 가 비었습니다."
case "$DEPLOY_SSH_HOST" in
  *[[:space:]]* | *@* | */*) fail "DEPLOY_SSH_HOST 에는 호스트 이름이나 IP 만 넣습니다(공백·@·/ 불가)." ;;
esac
if ! [[ "$DEPLOY_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || [ "$DEPLOY_SSH_PORT" -lt 1 ] || [ "$DEPLOY_SSH_PORT" -gt 65535 ]; then
  fail "DEPLOY_SSH_PORT 는 1~65535 숫자여야 합니다."
fi
[[ "$DEPLOY_SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "DEPLOY_SSH_USER 형식이 올바르지 않습니다."
[ -f "$DEPLOY_SSH_KEY_FILE" ] || fail "개인키 파일이 없습니다: $DEPLOY_SSH_KEY_FILE"
# CI 는 비대화형(BatchMode)이라 암호 걸린 키는 쓸 수 없다.
ssh-keygen -y -P "" -f "$DEPLOY_SSH_KEY_FILE" >/dev/null 2>&1 \
  || fail "암호 없는 OpenSSH 개인키가 아닙니다 — ssh-keygen -t ed25519 -N '' 로 만든 CI 전용 키를 쓰세요."
[ -s "$DEPLOY_SSH_KNOWN_HOSTS_FILE" ] || fail "known_hosts 파일이 없거나 비었습니다: $DEPLOY_SSH_KNOWN_HOSTS_FILE"
FINGERPRINTS="$(ssh-keygen -lf "$DEPLOY_SSH_KNOWN_HOSTS_FILE" 2>/dev/null)" \
  || fail "known_hosts 형식이 아닙니다: $DEPLOY_SSH_KNOWN_HOSTS_FILE"
# 포트가 22 가 아니면 ssh 는 known_hosts 에서 "[host]:port" 로 찾는다.
if [ "$DEPLOY_SSH_PORT" = "22" ]; then
  KNOWN_HOST_KEY="$DEPLOY_SSH_HOST"
else
  KNOWN_HOST_KEY="[$DEPLOY_SSH_HOST]:$DEPLOY_SSH_PORT"
fi
ssh-keygen -F "$KNOWN_HOST_KEY" -f "$DEPLOY_SSH_KNOWN_HOSTS_FILE" >/dev/null 2>&1 \
  || fail "known_hosts 에 $KNOWN_HOST_KEY 항목이 없습니다 — ssh-keyscan -p <port> <host> 를 DEPLOY_SSH_HOST 와 같은 이름으로 실행하세요."

echo "🔑 등록할 서버 호스트 키 지문(서버에서 직접 본 값과 같아야 합니다):"
printf '%s\n' "$FINGERPRINTS"

echo "🔐 시크릿 등록/갱신(gh secret set, idempotent)..."
# shellcheck disable=SC2086
printf '%s' "$DEPLOY_SSH_HOST" | gh secret set DEPLOY_SSH_HOST $REPO_FLAG
# shellcheck disable=SC2086
printf '%s' "$DEPLOY_SSH_PORT" | gh secret set DEPLOY_SSH_PORT $REPO_FLAG
# shellcheck disable=SC2086
printf '%s' "$DEPLOY_SSH_USER" | gh secret set DEPLOY_SSH_USER $REPO_FLAG
# 여러 줄 값은 파일을 그대로 stdin 으로 넘긴다(개행 보존).
# shellcheck disable=SC2086
gh secret set DEPLOY_SSH_KEY $REPO_FLAG < "$DEPLOY_SSH_KEY_FILE"
# shellcheck disable=SC2086
gh secret set DEPLOY_SSH_KNOWN_HOSTS $REPO_FLAG < "$DEPLOY_SSH_KNOWN_HOSTS_FILE"

echo "✅ 완료. 확인: ./scripts/setup_github_secrets.sh --check"
