"""시술 자동완료 대상 정책 회귀 테스트 — 직원이 방문 처리한 VISITED 만 자동완료한다.

대상:
- app/enum/treatment_status.py::TreatmentStatus.autocomplete_statuses
- app/crud/treatment_crud.py::get_treatments_to_autocomplete
- worker/tasks/treatment_task.py::auto_complete_treatment

결함:
- 자동완료가 미완료 상태(RESERVED/VISITED) 전체를 대상으로 했다. 직원이 처리하지 않은
  예약(RESERVED, 노쇼일 가능성이 크다)도 종료 시각이 지나면 COMPLETED 가 됐다. 결제 수단
  기본값이 CARD 라 실매출(COMPLETED + 카드/현금)에 잡히고 노쇼로는 집계되지 않았다.

요구 동작(제품 결정):
- 자동완료 대상은 직원이 방문 처리한 VISITED 뿐이다. 종료 시각이 지난 RESERVED 는
  RESERVED 로 남고, 직원이 방문/노쇼를 직접 고른다.
- 조회 조건과 UPDATE 재확인 조건 모두 자동완료 대상 상태(VISITED)를 쓴다. 조회와 UPDATE
  사이에 VISITED 가 아니게 된 예약(취소·노쇼·예약으로 되돌림)은 덮어쓰지 않는다.
- unfinished_statuses()(RESERVED/VISITED)는 다른 용도로 그대로 둔다.

tests/fake_sql.py 평가기로 실제 조회문과 UPDATE 조건을 DB 없이 실행한다.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.crud import treatment_crud
from app.enum.treatment_status import TreatmentStatus
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from tests.fake_sql import FakeSQLSession
from worker.tasks import treatment_task

# 태스크 비교 기준(UTC naive). reserved_at 컬럼도 naive UTC 로 저장된다.
NOW = datetime(2026, 10, 16, 12, 0)  # noqa: DTZ001  # naive UTC 컬럼 규약

RunTask = Callable[[FakeSQLSession], None]


def _past_end(treatment_id: int, status: TreatmentStatus) -> list[object]:
    """종료 시각이 이미 지난 예약 1건(3시간 전 시작 + 항목 60분)."""
    return [
        Treatment(
            id=treatment_id,
            shop_id=10,
            reserved_at=NOW - timedelta(hours=3),
            status=status,
            finished_at=None,
        ),
        TreatmentItem(
            id=treatment_id * 10,
            treatment_id=treatment_id,
            menu_detail_id=1,
            base_price=10000,
            duration_min=60,
            session_no=1,
        ),
    ]


def _row(db: FakeSQLSession, treatment_id: int) -> Treatment:
    return next(t for t in db.rows("treatment") if t.id == treatment_id)


@pytest.fixture
def run_task(monkeypatch: pytest.MonkeyPatch) -> RunTask:
    """가짜(fake) 세션과 고정된 현재 시각으로 자동완료 태스크를 실행하는 함수."""
    monkeypatch.setattr(treatment_task, "now_utc", lambda: NOW.replace(tzinfo=UTC))

    def _run(db: FakeSQLSession) -> None:
        monkeypatch.setattr(treatment_task, "SessionLocal", lambda: db)
        treatment_task.auto_complete_treatment()

    return _run


def test_autocomplete_statuses_is_visited_only() -> None:
    """자동완료 대상 상태는 VISITED 하나다. 미완료 상태 목록은 바뀌지 않는다."""
    assert TreatmentStatus.autocomplete_statuses() == [TreatmentStatus.VISITED]
    assert TreatmentStatus.unfinished_statuses() == [
        TreatmentStatus.RESERVED,
        TreatmentStatus.VISITED,
    ]


def test_reserved_past_end_is_not_a_candidate() -> None:
    """종료 시각이 지난 RESERVED 는 자동완료 조회 결과에 없다."""
    db = FakeSQLSession(*_past_end(1, TreatmentStatus.RESERVED))

    assert treatment_crud.get_treatments_to_autocomplete(db, NOW) == []


def test_reserved_past_end_stays_reserved_after_task(run_task: RunTask) -> None:
    """태스크가 돈 뒤에도 RESERVED 그대로이고 finished_at 은 NULL 이다."""
    db = FakeSQLSession(*_past_end(1, TreatmentStatus.RESERVED))

    run_task(db)

    reserved = _row(db, 1)
    assert reserved.status == TreatmentStatus.RESERVED
    assert reserved.finished_at is None


def test_visited_past_end_becomes_completed(run_task: RunTask) -> None:
    """직원이 방문 처리한 예약은 종료 시각이 지나면 COMPLETED 가 된다."""
    db = FakeSQLSession(*_past_end(1, TreatmentStatus.VISITED))

    run_task(db)

    visited = _row(db, 1)
    assert visited.status == TreatmentStatus.COMPLETED
    assert visited.finished_at == NOW


@pytest.mark.parametrize(
    "changed_to",
    [TreatmentStatus.CANCELLED, TreatmentStatus.NO_SHOW, TreatmentStatus.RESERVED],
    ids=["cancelled", "no-show", "back-to-reserved"],
)
def test_status_changed_between_select_and_update_is_not_overwritten(
    run_task: RunTask,
    monkeypatch: pytest.MonkeyPatch,
    changed_to: TreatmentStatus,
) -> None:
    """조회 직후 VISITED 가 아니게 된 예약은 UPDATE 가 COMPLETED 로 덮어쓰지 않는다."""
    db = FakeSQLSession(
        *_past_end(1, TreatmentStatus.VISITED),
        *_past_end(2, TreatmentStatus.VISITED),
    )
    real_select = treatment_crud.get_treatments_to_autocomplete

    def select_then_change(*args: object, **kwargs: object) -> list:
        rows = real_select(*args, **kwargs)
        # 다른 요청이 조회와 UPDATE 사이에 예약 1의 상태를 바꾼다.
        _row(db, 1).status = changed_to
        return rows

    monkeypatch.setattr(
        treatment_task,
        "get_treatments_to_autocomplete",
        select_then_change,
    )

    run_task(db)

    assert _row(db, 1).status == changed_to
    assert _row(db, 1).finished_at is None
    assert _row(db, 2).status == TreatmentStatus.COMPLETED
