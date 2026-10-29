"""인프라 정합성 가드 테스트 (SPEC-INFRA-001 · 운영 서버 docker compose 배포).

배포 산출물의 핵심 불변식을 정적·행위 수준에서 검증한다.
- `compose.prod.yaml` · `scripts/*.sh` · 워크플로에 가변 태그 `:latest` 가 없어야 한다
  (REQ-INFRA-003). 운영 정의는 `compose.prod.yaml` 하나다(Swarm 산출물은 없어야 한다).
- 외부 bridge `shared_network_prod` 하나만 쓰고 호스트 포트(`ports:`)를 공개하지 않으며,
  서비스 이름 = container_name = `kmcbeauty-*` 다(공유 네트워크에 일반 별칭 금지).
- 이미지는 `${IMAGE_TAG:?...}` 로 필수 주입하고 메모리 상한 · 재시작 · 로그 회전이 있다.
- 시크릿 파일 → target(Settings 필드명) 매핑이 정해진 대로이고
  `scripts/deploy.sh` 의 파일 목록과 같다. `/secrets/` 는 git 이 무시한다.
- 공개 저장소라 배포 산출물에 사설 IPv4 를 적지 않는다.
- `scripts/*.sh` 는 실행 가능해야 한다(첫 배포 차단 이슈 재발 방지).
- `cd.yml` 은 서드파티 SSH 액션 없이 OpenSSH + 고정 호스트 키로 배포한다.
- `scripts/deploy_entry.sh`(SSH 강제 명령)는 형식이 틀린 요청을 git·docker·파일을
  건드리기 전에 종료 코드 2 로 거부한다(PATH 스텁이 호출을 기록해 검증).

실제 서버/GitHub 환경 없이도 CI 에서 실행된다.
"""

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

# 저장소 루트: tests/ 의 부모 디렉토리
REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "compose.prod.yaml"
CD_FILE = REPO_ROOT / ".github" / "workflows" / "cd.yml"
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEPLOY_SCRIPT = SCRIPTS_DIR / "deploy.sh"
DEPLOY_ENTRY = SCRIPTS_DIR / "deploy_entry.sh"
ENV_PROD_EXAMPLE = REPO_ROOT / ".env.prod.example"
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "api-beauty.conf"
GITIGNORE = REPO_ROOT / ".gitignore"
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# `:latest` 잔존을 금지할 배포 산출물 목록.
GUARDED_FILES = [COMPOSE_FILE, *sorted(SCRIPTS_DIR.glob("*.sh")), *WORKFLOWS]
# Swarm 시절 산출물 — compose 로 옮기며 지웠다. 되살아나면 운영 정의가 둘이 된다.
REMOVED_SWARM_FILES = (
    "docker-stack.yml",
    "scripts/deploy_migrate.sh",
    "scripts/start_swarm.sh",
    "docs/scaling.md",
)
# 공개 저장소에 올라가는 배포 문서·설정(사설 IPv4 금지 대상).
PUBLIC_DEPLOY_ARTIFACTS = [
    COMPOSE_FILE,
    ENV_PROD_EXAMPLE,
    NGINX_CONF,
    REPO_ROOT / "docs" / "deployment.md",
    REPO_ROOT / "readme.rst",
    REPO_ROOT / ".moai" / "project" / "tech.md",
    REPO_ROOT / ".moai" / "project" / "structure.md",
    *sorted(SCRIPTS_DIR.glob("*.sh")),
    *WORKFLOWS,
]

NETWORK = "shared_network_prod"
API = "kmcbeauty-api"
WORKER = "kmcbeauty-celery-worker"
BEAT = "kmcbeauty-celery-beat"
FIREBASE_TARGET = "firebase_service_account"
# Settings 필드명 = 시크릿 target. .env.prod 에 두면 env 가 /run/secrets 를 덮어쓴다.
SECRET_FIELDS = (
    "SECRET_KEY",
    "FERNET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "RATE_LIMIT_STORAGE_URI",
)
# compose 시크릿 이름 → (서버 파일, 컨테이너 target).
EXPECTED_SECRETS = {
    "secret_key": ("./secrets/secret_key", "SECRET_KEY"),
    "fernet_key": ("./secrets/fernet_key", "FERNET_KEY"),
    "database_url": ("./secrets/database_url", "DATABASE_URL"),
    "redis_url": ("./secrets/redis_url", "REDIS_URL"),
    "rate_limit_storage_uri": (
        "./secrets/rate_limit_storage_uri",
        "RATE_LIMIT_STORAGE_URI",
    ),
    "firebase_service_account": (
        "./secrets/firebase_service_account.json",
        FIREBASE_TARGET,
    ),
}
# 서비스별로 마운트해야 하는 시크릿 target(rate limit 저장소는 api 만).
EXPECTED_SECRET_TARGETS = {
    API: {FIREBASE_TARGET, *SECRET_FIELDS},
    WORKER: {FIREBASE_TARGET, *SECRET_FIELDS} - {"RATE_LIMIT_STORAGE_URI"},
    BEAT: {FIREBASE_TARGET, *SECRET_FIELDS} - {"RATE_LIMIT_STORAGE_URI"},
}
EXPECTED_MEM_LIMITS = {API: "512m", WORKER: "512m", BEAT: "256m"}
# 오류 메시지에 '-' 를 허용하지 않는다: docker CLI(stack)의 치환은 '-'(기본값) 규칙을
# ':?' 보다 먼저 적용해 "${IMAGE_TAG:?a-b}" 를 기본값 "b" 로 바꾼다
# (미지정이어도 렌더가 성공해 버린다). compose v2 는 영향이 없지만 안전하게 둔다.
IMAGE_TAG_PATTERN = re.compile(r"\$\{IMAGE_TAG:\?[^}\-]+\}")
# 사설 IPv4(10/8, 172.16/12, 192.168/16). 서버 내부 주소는 시크릿·서버 파일에만 둔다.
PRIVATE_IPV4 = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2})\b",
)


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


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _services() -> dict[str, dict[str, Any]]:
    return _load_yaml(COMPOSE_FILE)["services"]


def test_no_latest_tag_in_deployment_artifacts() -> None:
    """배포 정의/스크립트/워크플로에 `:latest` 가 하나도 남아 있지 않아야 한다.

    (REQ-INFRA-003)
    """
    offenders: list[str] = []
    for path in GUARDED_FILES:
        offenders.extend(_offending_lines(path))

    assert not offenders, (
        "가변 태그 `:latest` 가 배포 산출물에 존재합니다. "
        "불변 SHA 태그(${IMAGE_TAG})로 교체하세요.\n" + "\n".join(offenders)
    )


# --- compose.prod.yaml --------------------------------------------------------


def test_compose_file_exists_and_swarm_artifacts_removed() -> None:
    """운영 정의는 compose.prod.yaml 하나다 — Swarm 시절 파일은 남아 있지 않다."""
    assert COMPOSE_FILE.exists(), "compose.prod.yaml 이 존재하지 않습니다."
    leftovers = [name for name in REMOVED_SWARM_FILES if (REPO_ROOT / name).exists()]
    assert not leftovers, f"Swarm 산출물이 남아 있습니다: {leftovers}"
    assert _load_yaml(COMPOSE_FILE)["name"] == "kmcbeauty"


def test_compose_uses_only_external_shared_network_prod() -> None:
    """모든 서비스는 서버 인프라의 외부 bridge shared_network_prod 하나에만 붙는다."""
    compose = _load_yaml(COMPOSE_FILE)

    assert compose["networks"] == {NETWORK: {"external": True, "name": NETWORK}}
    for name, service in compose["services"].items():
        assert set(service.get("networks") or []) == {NETWORK}, name
        assert "network_mode" not in service, name


def test_compose_publishes_no_host_ports() -> None:
    """호스트 포트는 공개하지 않는다 — 유입은 공유 nginx 뿐(api 는 expose 8000)."""
    services = _services()

    assert not [name for name, svc in services.items() if "ports" in svc]
    assert services[API]["expose"] == ["8000"]


def test_compose_service_and_container_names_are_prefixed() -> None:
    """서비스 이름 = container_name = kmcbeauty-* (공유 네트워크의 DNS 별칭이 된다)."""
    services = _services()

    assert set(services) == {API, WORKER, BEAT}
    for name, service in services.items():
        assert name.startswith("kmcbeauty-"), name
        assert service["container_name"] == name


def test_compose_images_require_image_tag() -> None:
    """모든 서비스 이미지는 `${IMAGE_TAG:?메시지}` 하나뿐이다(기본값·`:latest` 없음)."""
    for name, service in _services().items():
        image = service["image"]
        assert IMAGE_TAG_PATTERN.fullmatch(image), (
            f"{name}: image 는 ${{IMAGE_TAG:?메시지}}(메시지에 '-' 없음): {image}"
        )
        assert ":latest" not in image


def test_compose_secret_files_and_targets() -> None:
    """시크릿은 ./secrets/<파일> → /run/secrets/<Settings 필드명> 으로만 마운트한다."""
    compose = _load_yaml(COMPOSE_FILE)
    top_level = compose["secrets"]

    assert {name: spec["file"] for name, spec in top_level.items()} == {
        name: file for name, (file, _) in EXPECTED_SECRETS.items()
    }
    for name, service in compose["services"].items():
        targets = set()
        for mount in service["secrets"]:
            assert isinstance(mount, dict), (
                f"{name}: 짧은 문법 {mount!r} — source/target 을 명시하세요."
            )
            source, target = mount["source"], mount["target"]
            assert EXPECTED_SECRETS[source][1] == target, f"{name}: {source} → {target}"
            targets.add(target)
        assert targets == EXPECTED_SECRET_TARGETS[name], name
        assert service["environment"]["FIREBASE_SERVICE_ACCOUNT_KEY_PATH"] == (
            f"/run/secrets/{FIREBASE_TARGET}"
        )


def test_compose_memory_limits_and_runtime_settings() -> None:
    """메모리 상한 · 재시작 · 로그 회전 · 헬스체크 · 워커 동시성이 정해진 대로다."""
    services = _services()

    for name, service in services.items():
        assert service["mem_limit"] == EXPECTED_MEM_LIMITS[name], name
        assert service["restart"] == "unless-stopped", name
        assert service["env_file"] == [".env.prod"], name
        assert service["environment"]["LOG_TO_FILE"] == "false", name
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }, name
    # api 는 이미지의 HEALTHCHECK(curl /health)를 쓰고, celery 는 그것을 끈다.
    assert "healthcheck" not in services[API]
    assert services[WORKER]["healthcheck"] == {"disable": True}
    assert services[BEAT]["healthcheck"] == {"disable": True}
    assert "--concurrency=2" in services[WORKER]["command"]


def test_deploy_secret_files_match_compose() -> None:
    """deploy.sh 가 검사하는 시크릿 파일 목록이 compose 의 secrets 파일과 같다."""
    compose_files = {
        Path(spec["file"]).name for spec in _load_yaml(COMPOSE_FILE)["secrets"].values()
    }
    match = re.search(
        r"^SECRET_FILES=\(([^)]*)\)",
        DEPLOY_SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )

    assert match, "scripts/deploy.sh 에 SECRET_FILES=(...) 가 없습니다."
    assert set(match.group(1).split()) == compose_files


def test_nginx_conf_targets_api_container() -> None:
    """원본 nginx 설정이 api 컨테이너 이름·포트를 요청 시점에 해석한다."""
    conf = NGINX_CONF.read_text(encoding="utf-8")

    assert f"set $kmc_api http://{API}:8000;" in conf
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in conf
    assert "proxy_pass $kmc_api;" in conf
    assert "shared_overlay_prod" not in conf


def test_env_prod_example_keeps_secrets_out() -> None:
    """`.env.prod.example` 를 복사해도 deploy.sh 의 시크릿 키 검사에 걸리지 않는다.

    nginx 주소는 공개 저장소에 적지 않으므로 FORWARDED_ALLOW_IPS 는 자리표시자여야 한다.
    """
    lines = ENV_PROD_EXAMPLE.read_text(encoding="utf-8").splitlines()
    secret_definition = re.compile(
        rf"^\s*(export\s+)?({'|'.join(SECRET_FIELDS)})\s*(=|$)",
    )

    offending = [line for line in lines if secret_definition.match(line)]
    assert not offending, offending
    assert "APP_ENV=production" in lines
    forwarded = [line for line in lines if line.startswith("FORWARDED_ALLOW_IPS=")]
    assert len(forwarded) == 1
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", forwarded[0]), forwarded[0]


def test_secrets_dir_is_gitignored() -> None:
    """서버의 시크릿 파일 디렉터리는 절대 커밋되지 않는다."""
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()

    assert "/secrets/" in [line.strip() for line in lines]


def test_no_private_ipv4_in_public_deploy_artifacts() -> None:
    """공개 저장소의 배포 산출물에 사설 IPv4(서버 내부 주소)가 없다."""
    offenders = []
    for path in PUBLIC_DEPLOY_ARTIFACTS:
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if PRIVATE_IPV4.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    assert not offenders, offenders


# --- scripts/*.sh 실행 비트 ---------------------------------------------------


def _git_index_modes() -> dict[str, str]:
    """추적 중인 scripts/*.sh 의 git 인덱스 모드(git·저장소가 없으면 빈 dict)."""
    git = shutil.which("git")
    if git is None:
        return {}
    # 인자는 고정 리터럴뿐이라 셸 주입 여지가 없다.
    result = subprocess.run(  # noqa: S603
        [git, "ls-files", "-s", "--", "scripts/*.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    # 출력 형식: "<mode> <object> <stage>\t<path>"
    return {
        path: meta.split()[0]
        for meta, _, path in (
            line.partition("\t") for line in result.stdout.splitlines()
        )
    }


def test_shell_scripts_are_executable() -> None:
    """scripts/*.sh 는 모두 실행 가능해야 한다.

    - 추적 중인 파일: git 인덱스 모드가 100755 (커밋·체크아웃되는 모드가 기준).
    - 아직 추적하지 않는 새 파일: 디스크 실행 비트(git add 시 100755 로 기록된다).
    100644 면 서버의 `./scripts/...` 호출과 SSH 강제 명령(deploy_entry.sh)이
    Permission denied 로 실패한다. 지운 스크립트(인덱스에만 남은 것)는 건너뛴다.
    """
    scripts = sorted(SCRIPTS_DIR.glob("*.sh"))
    assert scripts, "scripts/*.sh 가 없습니다(가드가 공허함)."
    index_modes = _git_index_modes()

    problems = []
    for path in scripts:
        rel = path.relative_to(REPO_ROOT).as_posix()
        mode = index_modes.get(rel)
        if mode is not None:
            if mode != "100755":
                problems.append(f"{rel}: git 모드 {mode}")
        elif sys.platform != "win32" and not path.stat().st_mode & stat.S_IXUSR:
            problems.append(f"{rel}: 미추적 파일인데 디스크 실행 비트가 없음")

    assert not problems, (
        "실행 비트가 없는 스크립트가 있습니다. "
        "`chmod +x` 후 `git add` 하세요(또는 `git update-index --chmod=+x`):\n"
        + "\n".join(problems)
    )


# --- .github/workflows/cd.yml ------------------------------------------------


def test_cd_deploys_with_plain_openssh_and_pinned_host_key() -> None:
    """배포 잡은 서드파티 SSH 액션 없이 OpenSSH 를 쓰고 호스트 키 검증을 끄지 않는다."""
    workflow = _load_yaml(CD_FILE)
    uses = [
        step.get("uses", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    deploy_script = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"]
    )

    assert "appleboy/ssh-action" not in CD_FILE.read_text(encoding="utf-8")
    assert not [u for u in uses if u.startswith("appleboy/")], uses
    assert "StrictHostKeyChecking=yes" in deploy_script
    assert "BatchMode=yes" in deploy_script
    assert "StrictHostKeyChecking=no" not in deploy_script
    assert "accept-new" not in deploy_script


# --- scripts/deploy_entry.sh (SSH 강제 명령) 행위 테스트 ----------------------

_BASH = shutil.which("bash")
_requires_bash = pytest.mark.skipif(
    _BASH is None or sys.platform == "win32",
    reason="bash 가 필요합니다(POSIX 셸 스크립트 행위 테스트).",
)
_SHA = "0123456789abcdef0123456789abcdef01234567"
# 호출되면 STUB_LOG 에 기록만 하는 가짜 명령. git merge-base 의 결과는 env 로 조절한다.
_STUBBED_COMMANDS = ("git", "docker", "flock", "curl", "ssh", "id")


class _EntrySandbox(NamedTuple):
    bin_dir: Path
    repo: Path
    log: Path


@pytest.fixture
def entry_sandbox(tmp_path: Path) -> _EntrySandbox:
    """deploy_entry.sh 사본 + 가짜 deploy.sh 가 든 격리 저장소와 PATH 스텁을 만든다."""
    bin_dir = tmp_path / "bin"
    repo = tmp_path / "repo"
    bin_dir.mkdir()
    (repo / ".git").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy2(DEPLOY_ENTRY, repo / "scripts" / "deploy_entry.sh")
    (repo / "scripts" / "deploy.sh").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "deploy.sh $* user=$REGISTRY_USER token=$REGISTRY_TOKEN'
        ' lock_fd=$KMC_DEPLOY_LOCK_FD" >> "$STUB_LOG"\n',
        encoding="utf-8",
    )
    for name in _STUBBED_COMMANDS:
        stub = bin_dir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "{name} $*" >> "$STUB_LOG"\n'
            f'if [ "{name}" = git ] && [ "$1" = merge-base ]; then\n'
            '  exit "${STUB_MERGE_BASE_RC:-0}"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return _EntrySandbox(bin_dir=bin_dir, repo=repo, log=tmp_path / "calls.log")


def _run_entry(
    sandbox: _EntrySandbox,
    command: str | None,
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """SSH_ORIGINAL_COMMAND 를 sshd 처럼 넘겨 deploy_entry.sh 사본을 실행한다."""
    env = {
        "PATH": f"{sandbox.bin_dir}:/usr/bin:/bin",
        "HOME": str(sandbox.repo.parent),
        "STUB_LOG": str(sandbox.log),
        "DEPLOY_DIR": str(sandbox.repo),
        **(extra_env or {}),
    }
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    # 실행 파일은 which 로 찾은 bash 절대 경로, 인자는 테스트가 만든 경로뿐이다.
    return subprocess.run(  # noqa: S603
        [str(_BASH), str(sandbox.repo / "scripts" / "deploy_entry.sh")],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def _calls(sandbox: _EntrySandbox) -> list[str]:
    if not sandbox.log.exists():
        return []
    return sandbox.log.read_text(encoding="utf-8").splitlines()


@_requires_bash
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("deploy", id="no-sha"),
        pytest.param(f"deploy {_SHA} extra", id="extra-args"),
        pytest.param(f"deploy {_SHA[:39]}", id="short-sha"),
        pytest.param(f"deploy {_SHA}0", id="long-sha"),
        pytest.param(f"deploy {_SHA.upper()}", id="uppercase-sha"),
        pytest.param("deploy $(id)", id="command-substitution"),
        pytest.param(f"deploy {_SHA};id", id="semicolon-suffix"),
        pytest.param(";", id="semicolon"),
        pytest.param(f"deploy  {_SHA}", id="double-space"),
        pytest.param(f"Deploy {_SHA}", id="capitalized-verb"),
        pytest.param(f"deploy {_SHA}\n", id="trailing-newline"),
        pytest.param(f"id\ndeploy {_SHA}", id="leading-line"),
        pytest.param(f"rollback {_SHA}", id="other-verb"),
    ],
)
def test_deploy_entry_rejects_malformed_requests(
    entry_sandbox: _EntrySandbox,
    command: str | None,
) -> None:
    """형식이 틀린 요청은 종료 코드 2 — 명령 실행·파일 생성이 전혀 없다."""
    before = _tree(entry_sandbox.repo)

    result = _run_entry(entry_sandbox, command)

    assert result.returncode == 2, result.stderr
    assert _calls(entry_sandbox) == []
    assert _tree(entry_sandbox.repo) == before


@_requires_bash
def test_deploy_entry_rejects_malformed_token_before_git(
    entry_sandbox: _EntrySandbox,
) -> None:
    """토큰(stdin 첫 줄) 형식이 틀리면 git·잠금 이전에 거부한다."""
    before = _tree(entry_sandbox.repo)

    result = _run_entry(entry_sandbox, f"deploy {_SHA}", stdin="bad token;id\n")

    assert result.returncode == 2, result.stderr
    assert _calls(entry_sandbox) == []
    assert _tree(entry_sandbox.repo) == before


@_requires_bash
def test_deploy_entry_checks_out_main_commit_then_execs_deploy(
    entry_sandbox: _EntrySandbox,
) -> None:
    """유효한 요청: 잠금 → fetch main → 조상 확인 → detach 체크아웃 → deploy.sh exec."""
    result = _run_entry(entry_sandbox, f"deploy {_SHA}", stdin="ghs_TestToken123\n")

    assert result.returncode == 0, result.stderr
    calls = _calls(entry_sandbox)
    git_calls = [call for call in calls if call.startswith("git ")]
    ancestor_check = f"git merge-base --is-ancestor {_SHA} origin/main"
    checkout = f"git checkout --force --detach {_SHA}"
    assert calls[0] == "flock -n 9"
    assert git_calls[0] == "git fetch --prune origin main"
    assert git_calls.index(ancestor_check) < git_calls.index(checkout)
    assert git_calls[-1] == checkout
    assert calls[-1] == (
        f"deploy.sh {_SHA} user=antam2010 token=ghs_TestToken123 lock_fd=9"
    )
    assert not [call for call in calls if call.startswith(("docker ", "id"))]


@_requires_bash
def test_deploy_entry_refuses_commit_not_on_main(
    entry_sandbox: _EntrySandbox,
) -> None:
    """origin/main 의 조상이 아닌 커밋은 체크아웃·배포하지 않는다."""
    result = _run_entry(
        entry_sandbox,
        f"deploy {_SHA}",
        extra_env={"STUB_MERGE_BASE_RC": "1"},
    )

    assert result.returncode == 2, result.stderr
    calls = _calls(entry_sandbox)
    assert not [call for call in calls if call.startswith("git checkout")]
    assert not [call for call in calls if call.startswith("deploy.sh")]
