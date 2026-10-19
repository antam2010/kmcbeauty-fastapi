"""인프라 정합성 가드 테스트 (SPEC-INFRA-001).

배포 하드닝의 핵심 불변식(REQ-INFRA-003)을 정적으로 검증한다.
- `docker-stack.yml` 및 `scripts/*.sh` 에 가변 태그 `:latest` 가 존재하면 안 된다.
  (latest-tag digest 함정 → 무중단 롤링 배포가 깨진다. acceptance.md 엣지 케이스 참조)
- `scripts/*.sh` 는 git 에 실행 비트(100755)로 추적되어야 한다. 100644 면 서버의
  `./scripts/...` 호출이 Permission denied 로 실패한다(첫 Swarm 배포 차단 이슈).

이 테스트는 실제 Swarm/GitHub 환경 없이도 CI 에서 grep 0건 조건(DoD)을 강제한다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

# 저장소 루트: tests/ 의 부모 디렉토리
REPO_ROOT = Path(__file__).resolve().parent.parent

# `:latest` 잔존을 금지할 배포 산출물 목록.
GUARDED_FILES = [
    REPO_ROOT / "docker-stack.yml",
    *sorted((REPO_ROOT / "scripts").glob("*.sh")),
    *sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")),
]


def _offending_lines(path: Path) -> list[str]:
    """파일에서 `:latest` 를 포함한 라인을 (라인번호, 내용) 형태로 수집한다."""
    if not path.exists():
        return []
    offending = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if ":latest" in line:
            offending.append(f"{path.name}:{lineno}: {line.strip()}")
    return offending


def test_no_latest_tag_in_deployment_artifacts() -> None:
    """배포 스택/스크립트에 `:latest` 가 하나도 남아 있지 않아야 한다.

    (REQ-INFRA-003)
    """
    offenders: list[str] = []
    for path in GUARDED_FILES:
        offenders.extend(_offending_lines(path))

    assert not offenders, (
        "가변 태그 `:latest` 가 배포 산출물에 존재합니다. "
        "불변 SHA 태그(${IMAGE_TAG})로 교체하세요.\n" + "\n".join(offenders)
    )


def test_guarded_files_exist() -> None:
    """가드 대상 핵심 파일(docker-stack.yml)이 실제로 존재하는지 확인한다."""
    assert (REPO_ROOT / "docker-stack.yml").exists(), (
        "docker-stack.yml 이 존재하지 않습니다."
    )


def test_shell_scripts_tracked_as_executable() -> None:
    """추적 중인 scripts/*.sh 는 모두 git 모드 100755 여야 한다.

    작업 트리가 아니라 인덱스(`git ls-files -s`)를 본다 — 커밋·체크아웃되는 모드가
    기준이기 때문이다. git 이 없거나 저장소가 아닌 환경(예: 이미지 내부)에서는 건너뛴다.
    """
    git = shutil.which("git")
    if git is None:
        pytest.skip("git 미설치 — 실행 비트 가드를 건너뜁니다.")
    # 인자는 고정 리터럴뿐이라 셸 주입 여지가 없다.
    result = subprocess.run(  # noqa: S603
        [git, "ls-files", "-s", "--", "scripts/*.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"git 저장소가 아님 — 실행 비트 가드를 건너뜁니다: {result.stderr}")

    # 출력 형식: "<mode> <object> <stage>\t<path>"
    modes = {
        path: meta.split()[0]
        for meta, _, path in (
            line.partition("\t") for line in result.stdout.splitlines()
        )
    }
    assert modes, "git 이 추적하는 scripts/*.sh 가 없습니다(가드가 공허함)."
    not_executable = sorted(path for path, mode in modes.items() if mode != "100755")
    assert not not_executable, (
        "실행 비트 없이 추적되는 스크립트가 있습니다. "
        "`chmod +x` 후 `git add` 하세요(또는 `git update-index --chmod=+x`):\n"
        + "\n".join(not_executable)
    )
