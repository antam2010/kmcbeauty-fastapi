"""scripts/prune_artifacts.sh 행위 테스트 (배포 뒤 덤프 · 이미지 정리).

사용자 결정(2026-11-06): 배포에 성공하면 이 DB 의 덤프는 최신 10개, kmcbeauty-api
이미지는 현재 · 직전(롤백용) 2개만 남긴다. 덤프 디렉터리는 공유 PostgreSQL 컨테이너의
것이라 다른 앱 · 다른 형식의 파일은 절대 지우지 않는다.

가짜 docker 를 PATH 앞에 둔다.
- `docker exec <컨테이너> CMD...` → 컨테이너 대신 이 머신에서 CMD 를 실행한다.
- `docker image ls` → STUB_IMAGES 파일 내용을 출력한다.
- `docker rmi IMG` → 호출을 STUB_LOG 에 남기고, STUB_RMI_FAIL 과 같으면 실패한다.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRUNE_SCRIPT = REPO_ROOT / "scripts" / "prune_artifacts.sh"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None or sys.platform == "win32",
    reason="bash 가 필요합니다(POSIX 셸 스크립트 행위 테스트).",
)

REPO = "ghcr.io/antam2010/kmcbeauty-api"
CUR = f"{REPO}:{'c' * 40}"
PREV = f"{REPO}:{'b' * 40}"
OLD = [f"{REPO}:{ch * 40}" for ch in "0123"]

_FAKE_DOCKER = """#!/bin/sh
case "$1" in
  exec) shift 2; exec "$@" ;;
  image) cat "$STUB_IMAGES"; exit 0 ;;
  rmi)
    printf '%s\\n' "rmi $2" >> "$STUB_LOG"
    [ "$2" = "${STUB_RMI_FAIL:-}" ] && exit 1
    exit 0 ;;
esac
exit 99
"""


class _Sandbox(NamedTuple):
    bin_dir: Path
    backup: Path
    images: Path
    log: Path


@pytest.fixture
def sandbox(tmp_path: Path) -> _Sandbox:
    """가짜 docker 와 덤프 디렉터리를 만든다."""
    bin_dir = tmp_path / "bin"
    backup = tmp_path / "backup"
    bin_dir.mkdir()
    backup.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    images = tmp_path / "images.txt"
    images.write_text("", encoding="utf-8")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    return _Sandbox(bin_dir=bin_dir, backup=backup, images=images, log=log)


def _dump(day: int) -> str:
    return f"kmcbeauty-202611{day:02d}-120000.dump"


def _run(
    sb: _Sandbox,
    keep: str = "10",
    prev: str | None = PREV,
    rmi_fail: str = "",
) -> subprocess.CompletedProcess[str]:
    args = [str(sb.backup), "kmcbeauty", keep, REPO, CUR]
    if prev is not None:
        args.append(prev)
    # 실행 파일은 which 로 찾은 bash 절대 경로, 인자는 테스트가 만든 값뿐이다.
    return subprocess.run(  # noqa: S603
        [str(_BASH), str(PRUNE_SCRIPT), "postgresql", *args],
        env={
            "PATH": f"{sb.bin_dir}:/usr/bin:/bin",
            "STUB_IMAGES": str(sb.images),
            "STUB_LOG": str(sb.log),
            "STUB_RMI_FAIL": rmi_fail,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def test_keeps_newest_dumps_of_this_db_only(sandbox: _Sandbox) -> None:
    """이 DB 의 덤프만 최신 N개를 남기고, 다른 앱 · 다른 형식 파일은 그대로 둔다."""
    dumps = [_dump(d) for d in range(1, 16)]  # 15개
    others = [
        "otherapp-20261101-120000.dump",
        "kmcbeauty-old-20261101-120000.dump",
        "kmcbeauty-manual.dump",
        "kmcbeauty-20261101-120000.sql",
    ]
    for name in dumps + others:
        (sandbox.backup / name).write_text("x", encoding="utf-8")

    result = _run(sandbox, keep="10")

    assert result.returncode == 0, result.stderr
    remaining = {p.name for p in sandbox.backup.iterdir()}
    assert remaining == set(dumps[5:]) | set(others)


def test_fewer_dumps_than_keep_deletes_nothing(sandbox: _Sandbox) -> None:
    """덤프가 N개 이하이면 하나도 지우지 않는다(디렉터리가 비어도 성공)."""
    for d in (1, 2, 3):
        (sandbox.backup / _dump(d)).write_text("x", encoding="utf-8")

    assert _run(sandbox).returncode == 0
    assert len(list(sandbox.backup.iterdir())) == 3

    for p in sandbox.backup.iterdir():
        p.unlink()
    assert _run(sandbox).returncode == 0


def test_removes_only_old_sha_images(sandbox: _Sandbox) -> None:
    """현재 · 직전 이미지와 sha 가 아닌 태그는 남기고, 나머지 sha 태그만 지운다."""
    sandbox.images.write_text(
        "\n".join([CUR, PREV, *OLD, f"{REPO}:latest", f"{REPO}:<none>"]) + "\n",
        encoding="utf-8",
    )

    result = _run(sandbox)

    assert result.returncode == 0, result.stderr
    calls = sandbox.log.read_text(encoding="utf-8").splitlines()
    assert sorted(calls) == sorted(f"rmi {img}" for img in OLD)


@pytest.mark.parametrize("prev", [None, "", CUR], ids=["no-arg", "empty", "same-sha"])
def test_unknown_or_same_prev_keeps_all_images(
    sandbox: _Sandbox,
    prev: str | None,
) -> None:
    """직전 이미지를 모르거나 현재와 같으면(같은 sha 재배포) 이미지를 지우지 않는다."""
    sandbox.images.write_text(f"{CUR}\n{OLD[0]}\n", encoding="utf-8")

    assert _run(sandbox, prev=prev).returncode == 0
    assert sandbox.log.read_text(encoding="utf-8") == ""


def test_keep_one_keeps_newest_and_ignores_dump_named_dirs(sandbox: _Sandbox) -> None:
    """keep=1 이면 최신 덤프만 남는다. 덤프 이름의 디렉터리와 그 안은 그대로 둔다."""
    for d in (1, 2, 3):
        (sandbox.backup / _dump(d)).write_text("x", encoding="utf-8")
    trap = sandbox.backup / _dump(31)  # 최신 이름이지만 디렉터리
    trap.mkdir()
    (trap / "victim").write_text("x", encoding="utf-8")
    (sandbox.backup / "victim").write_text("x", encoding="utf-8")

    assert _run(sandbox, keep="1").returncode == 0
    assert {p.name for p in sandbox.backup.iterdir()} == {_dump(3), trap.name, "victim"}
    assert (trap / "victim").exists()


def test_missing_backup_dir_reports_failure(sandbox: _Sandbox) -> None:
    """덤프 디렉터리가 없으면 종료 코드 1(배포 쪽은 경고만 낸다)."""
    sandbox.backup.rmdir()

    assert _run(sandbox).returncode == 1


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() == 0,
    reason="root 는 쓰기 금지 디렉터리에서도 지울 수 있다",
)
def test_rm_failure_reports_failure(sandbox: _Sandbox) -> None:
    """지우지 못한 덤프가 있으면(권한) 종료 코드 1 로 알린다."""
    for d in range(1, 13):
        (sandbox.backup / _dump(d)).write_text("x", encoding="utf-8")
    sandbox.backup.chmod(0o555)
    try:
        assert _run(sandbox).returncode == 1
    finally:
        sandbox.backup.chmod(0o755)


def test_rmi_failure_reports_but_continues(sandbox: _Sandbox) -> None:
    """이미지 하나를 못 지워도 나머지는 계속 지우고 종료 코드 1 로 알린다."""
    sandbox.images.write_text("\n".join([CUR, *OLD[:2]]) + "\n", encoding="utf-8")

    result = _run(sandbox, rmi_fail=OLD[0])

    assert result.returncode == 1
    assert f"rmi {OLD[1]}" in sandbox.log.read_text(encoding="utf-8")


@pytest.mark.parametrize("keep", ["0", "-1", "abc", ""])
def test_rejects_invalid_keep(sandbox: _Sandbox, keep: str) -> None:
    """보존 개수는 1 이상의 정수만 받는다(이번 배포의 덤프까지 지우지 않도록)."""
    (sandbox.backup / _dump(1)).write_text("x", encoding="utf-8")

    assert _run(sandbox, keep=keep).returncode == 2
    assert (sandbox.backup / _dump(1)).exists()


def test_deploy_calls_prune_after_smoke_and_tolerates_failure() -> None:
    """deploy.sh 는 스모크 뒤에 정리를 부르고, 정리 실패로 배포를 실패시키지 않는다."""
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    smoke = deploy.index("[7] 스모크")
    prune = deploy.index("scripts/prune_artifacts.sh")

    assert smoke < prune < deploy.index("[9] 요약")
    assert "if ! bash scripts/prune_artifacts.sh" in deploy
    call = " ".join(deploy[prune:].split("\n", 2)[:2]).replace("\\", " ")
    assert call.split()[1:8] == [
        '"$PG_CONTAINER"',
        '"$PG_BACKUP_DIR"',
        '"$PG_DB"',
        '"$KEEP_DUMPS"',
        '"$IMAGE_REPO"',
        '"$IMAGE"',
        '"$PREV_IMAGE";',
    ]
