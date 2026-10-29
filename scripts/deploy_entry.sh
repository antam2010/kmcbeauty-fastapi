#!/usr/bin/env bash
# ==============================================================================
# CD 전용 SSH 강제 명령(forced command) 진입점
#
# CI 배포 키로는 이 스크립트만 실행된다. 배포 계정의 ~/.ssh/authorized_keys 에 한 줄로 등록한다
# (공개키 부분은 실제 CI 공개키로 바꾼다 — docs/deployment.md 10절 (e)):
#   restrict,command="/srv/server-mini2/web/kmcbeauty-fastapi/scripts/deploy_entry.sh" ssh-ed25519 AAAA... kmcbeauty-cd
#
# 요청 형식: ssh 원격 명령이 정확히 "deploy <40자 소문자 hex 커밋 sha>" 여야 한다.
#   - sshd 가 그 문자열을 $SSH_ORIGINAL_COMMAND 로 넘긴다. 형식이 다르면 git·docker·파일을
#     건드리기 전에 종료 코드 2 로 거부한다. 값은 정규식으로만 검사하고 절대 실행·eval 하지 않는다.
#   - stdin 첫 줄은 GHCR pull 토큰(비어 있을 수 있음, CD 는 잡의 GITHUB_TOKEN)이다.
#
# 동작(요청이 유효할 때):
#   잠금(.git/kmcbeauty-deploy.lock) → git fetch --prune origin main → sha 가 origin/main 의
#   조상인지 확인 → git checkout --force --detach <sha> → 그 커밋의 scripts/deploy.sh <sha> 를 exec.
#   잠금 fd(9)는 exec 뒤에도 유지되어 deploy.sh 가 끝날 때까지 다른 배포를 막는다.
#
# DEPLOY_DIR: 기본 /srv/server-mini2/web/kmcbeauty-fastapi (위 command= 경로와 같은 clone).
#   sshd 는 클라이언트 환경변수를 기본으로 넘기지 않으므로(AcceptEnv) 운영에서는 기본값이 쓰인다.
#   clone 위치를 바꾸면 이 기본값과 authorized_keys 의 command 경로를 함께 바꾼다.
#
# 종료 코드: 2 요청 거부·사전 실패(형식·토큰 형식·잠금·fetch·main 조상 아님)
#           그 밖에는 exec 된 deploy.sh 의 종료 코드(0/2/3/4/5/6 — deploy.sh 머리말 참조).
# ==============================================================================
set -euo pipefail
# 정규식 범위([0-9a-f] 등)를 로케일과 무관하게 ASCII 로 고정한다(클라이언트가 LANG/LC_* 를 보낼 수 있다).
export LC_ALL=C

reject() {
  echo "deploy_entry: $1" >&2
  exit 2
}

# 함수로 감싸 끝까지 읽은 뒤 실행한다 — 아래 git checkout 이 이 파일을 바꿔도 실행 중인 내용은 그대로다.
main() {
  # ── [1] 요청 검증: 이 단계 전에는 git·docker·파일시스템을 건드리지 않는다 ──
  local request="${SSH_ORIGINAL_COMMAND:-}"
  # 전체 문자열 일치(^…$, 줄바꿈·공백·셸 메타문자가 끼면 불일치).
  local request_re='^deploy ([0-9a-f]{40})$'
  if ! [[ "$request" =~ $request_re ]]; then
    reject "허용되지 않는 요청입니다. 형식: deploy <40자 소문자 hex 커밋 sha>"
  fi
  local sha="${BASH_REMATCH[1]}"

  # stdin 첫 줄 = 레지스트리 토큰(없을 수 있음). 입력이 없으면 30초 뒤 빈 값으로 진행한다.
  local token=""
  IFS= read -r -t 30 token || true
  token="${token%$'\r'}"
  local token_re='^[A-Za-z0-9_]{0,255}$'
  [[ "$token" =~ $token_re ]] || reject "레지스트리 토큰 형식이 올바르지 않습니다."

  # ── [2] 배포 디렉터리 · 잠금 ──
  local deploy_dir="${DEPLOY_DIR:-/srv/server-mini2/web/kmcbeauty-fastapi}"
  cd "$deploy_dir" || reject "배포 디렉터리가 없습니다: $deploy_dir"
  [ -d .git ] || reject "배포 디렉터리가 git clone 이 아닙니다: $deploy_dir"
  exec 9>>.git/kmcbeauty-deploy.lock || reject "잠금 파일을 열 수 없습니다."
  flock -n 9 || reject "다른 배포가 진행 중입니다."
  echo "deploy_entry: 요청 수락 — ${sha}"

  # ── [3] main 에 있는 커밋만 체크아웃 ──
  git fetch --prune origin main || reject "git fetch origin main 실패"
  git cat-file -e "${sha}^{commit}" 2>/dev/null || reject "origin/main 에서 찾을 수 없는 커밋입니다: ${sha}"
  git merge-base --is-ancestor "$sha" origin/main || reject "origin/main 의 조상이 아닌 커밋은 배포하지 않습니다: ${sha}"
  git cat-file -e "${sha}:scripts/deploy.sh" 2>/dev/null \
    || reject "이 커밋에는 scripts/deploy.sh 가 없습니다(이 배포 방식 이전 커밋): ${sha}"
  git checkout --force --detach "$sha" || reject "git checkout 실패: ${sha}"

  # ── [4] 그 커밋의 deploy.sh 로 교체 실행(잠금 fd 9 를 넘긴다) ──
  export REGISTRY_TOKEN="$token" REGISTRY_USER=antam2010 KMC_DEPLOY_LOCK_FD=9
  exec bash scripts/deploy.sh "$sha"
}

main "$@"
