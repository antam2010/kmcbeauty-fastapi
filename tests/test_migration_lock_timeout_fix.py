"""마이그레이션 lock_timeout 가드 테스트 (운영 배포).

대상: alembic/env.py::run_migrations_online

결함: scripts/deploy.sh 는 compose up 전에 일회성 kmcbeauty-migrate 컨테이너에서
`alembic upgrade head` 를 돌린다. lock_timeout 이 없으면 테이블 잠금을 기다리는
마이그레이션(ALTER TABLE 등) 뒤로 그 테이블의 다른 모든 쿼리가 줄을 서 운영 API 가
멈춘다.

요구 동작(사용자 결정): 온라인 마이그레이션만 lock_timeout 10s(이름 있는 상수). 넘으면
마이그레이션이 실패하고 deploy.sh 가 compose up 전에 멈춰 기존 컨테이너가 계속
서비스한다. 오프라인 모드(SQL 스크립트 생성)는 그대로다.

env.py 를 import 하면 마이그레이션이 실행되므로 소스를 정적으로(ast) 검사한다.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PY = REPO_ROOT / "alembic" / "env.py"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"


def _module() -> ast.Module:
    return ast.parse(ENV_PY.read_text(encoding="utf-8"))


def _constants(module: ast.Module) -> dict[str, object]:
    """모듈 최상위의 `이름 = 리터럴` 상수."""
    constants: dict[str, object] = {}
    for node in module.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        try:
            constants[node.targets[0].id] = ast.literal_eval(node.value)
        except ValueError:
            continue  # 리터럴이 아닌 값(config 객체 등)
    return constants


def _function(module: ast.Module, name: str) -> ast.FunctionDef:
    [function] = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    return function


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == name
    ]


def _resolve(node: ast.expr, constants: dict[str, object]) -> object:
    """리터럴 · 모듈 상수 이름 · 그 둘로 만든 dict / f-string 을 값으로 바꾼다."""
    if isinstance(node, ast.Name):
        return constants[node.id]
    if isinstance(node, ast.Dict):
        return {
            _resolve(key, constants): _resolve(value, constants)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(
                _resolve(
                    part.value if isinstance(part, ast.FormattedValue) else part,
                    constants,
                ),
            )
            for part in node.values
        )
    return ast.literal_eval(node)


def test_lock_timeout_is_a_named_constant() -> None:
    """잠금 대기 상한은 이름 있는 상수 10s 다."""
    assert _constants(_module()).get("MIGRATION_LOCK_TIMEOUT") == "10s"


def test_online_migrations_connect_with_lock_timeout() -> None:
    """온라인 마이그레이션 엔진은 세션 옵션으로 lock_timeout=10s 를 건다.

    connect_args(libpq options)로 걸어 SET 을 따로 실행하지 않는다. begin_transaction
    밖에서 SET 을 실행하면 SQLAlchemy 2.0 이 커밋되지 않는 트랜잭션을 자동 시작한다.
    """
    module = _module()
    [engine_call] = _calls(
        _function(module, "run_migrations_online"),
        "engine_from_config",
    )
    keywords = {keyword.arg: keyword.value for keyword in engine_call.keywords}

    assert "connect_args" in keywords, "engine_from_config 에 connect_args 가 없습니다."
    assert _resolve(keywords["connect_args"], _constants(module)) == {
        "options": "-c lock_timeout=10s",
    }


def test_offline_migrations_are_unchanged() -> None:
    """대조군: 오프라인 모드(SQL 스크립트 생성)에는 lock_timeout 을 넣지 않는다."""
    offline = ast.unparse(_function(_module(), "run_migrations_offline"))

    assert "lock_timeout" not in offline
    assert "MIGRATION_LOCK_TIMEOUT" not in offline


def test_deploy_migrates_before_compose_up_and_stops_on_failure() -> None:
    """lock_timeout 으로 마이그레이션이 실패하면 compose up 전에 배포가 멈춘다.

    상한을 넘긴 실패가 안전한 전제: deploy.sh 가 compose up 보다 먼저 마이그레이션을
    돌리고, 실패하면 die 로 끝나 기존 컨테이너를 바꾸지 않는다.
    """
    lines = DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()
    # 안내 문구(say ...)가 아니라 실제 명령 줄만 고른다.
    [migrate] = [
        i
        for i, line in enumerate(lines)
        if line.startswith("compose run ") and "alembic upgrade head" in line
    ]
    [up] = [i for i, line in enumerate(lines) if line.startswith("compose up ")]

    assert migrate < up
    assert lines[migrate].rstrip().endswith("\\")
    assert lines[migrate + 1].lstrip().startswith("|| die ")
