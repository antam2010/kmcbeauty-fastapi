"""SPEC-FIX-001 REQ-FIX-004 — 자동완료 태스크 타임존/NULL 가드 테스트.

대상: worker/tasks/treatment_task.py::auto_complete_treatment
- 경과 시술만 COMPLETED 로 갱신 (AC-004-1)
- 미경과 시술은 미갱신 (AC-004-2)
- total_duration_min == None (항목 없는 시술) 이어도 float(None) 크래시 없음 (AC-004-3)
- reserved_at(UTC-naive) 과 비교 기준(UTC-naive) 정합 → 9시간 오판정 없음 (AC-004-4)

DB/Celery 의존을 fake 로 대체하고, .update() 에 전달된 완료 대상 ID 집합을 관찰한다.
조회 함수는 판정 기준 시각(now)을 함께 받고, UPDATE 는 id 조건 뒤에 미완료 재검사
조건을 더 건다(SQL 판정·배치 루프는 test_treatment_autocomplete_batch_fix.py).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from worker.tasks import treatment_task


class FakeUpdateQuery:
    """db.query(Treatment).filter(...).update(...) 체인을 흉내내고 대상 ID 를 기록."""

    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def filter(self, criterion: object, *_rechecks: object) -> "FakeUpdateQuery":
        # 첫 조건 Treatment.id.in_(complete_ids) 의 우변 리터럴(ID 목록)을 추출한다.
        try:
            self._recorder["ids"] = list(criterion.right.value)
        except AttributeError:
            self._recorder["ids"] = "unparsed"
        return self

    def update(
        self,
        values: dict,
        synchronize_session: bool = False,  # noqa: ARG002  # Session.update API 시그니처 유지
    ) -> None:
        self._recorder["updated"] = True
        self._recorder["values"] = values


class FakeSession:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def query(self, _model: object) -> FakeUpdateQuery:
        return FakeUpdateQuery(self._recorder)

    def commit(self) -> None:
        self._recorder["committed"] = True

    def rollback(self) -> None:  # pragma: no cover - 실패 경로 미사용
        self._recorder["rolled_back"] = True

    def close(self) -> None:
        self._recorder["closed"] = True


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict:
    recorder: dict = {}
    fake_session = FakeSession(recorder)

    monkeypatch.setattr(treatment_task, "SessionLocal", lambda: fake_session)
    # 고정된 "현재 시각"(UTC-naive) 을 주입해 결정적 판정.
    # DTZ001: 태스크가 UTC-naive 로 비교하므로 의도적으로 naive datetime 을 만든다.
    fixed_now = datetime(2026, 7, 2, 12, 0, 0)  # noqa: DTZ001  # UTC-naive 의도
    # 태스크는 now_utc().replace(tzinfo=None) 로 UTC-naive 를 얻으므로,
    # now_utc 는 aware(UTC) 를 반환하도록 패치한다.
    monkeypatch.setattr(
        treatment_task,
        "now_utc",
        lambda: fixed_now.replace(tzinfo=UTC),
    )
    return {"recorder": recorder, "now": fixed_now}


def _row(
    treatment_id: int,
    reserved_at: datetime,
    duration: int | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        treatment_id=treatment_id,
        shop_id=1,  # 완료 후 상점별 대시보드 캐시 무효화에 쓰인다
        reserved_at=reserved_at,
        total_duration_min=duration,
    )


def test_only_elapsed_treatments_completed(
    patched: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-004-1/2: 종료 시각 경과 시술만 완료 대상에 포함된다."""
    now = patched["now"]
    rows = [
        # 경과: reserved 2시간 전 + 60분 → 이미 종료 → 완료 대상
        _row(1, now - timedelta(hours=2), 60),
        # 미경과: reserved 10분 전 + 60분 → 아직 진행 중 → 제외
        _row(2, now - timedelta(minutes=10), 60),
    ]
    monkeypatch.setattr(
        treatment_task,
        "get_treatments_to_autocomplete",
        lambda _db, _now: rows,
    )

    treatment_task.auto_complete_treatment()

    assert patched["recorder"]["ids"] == [1]
    assert patched["recorder"]["committed"] is True


def test_none_duration_does_not_crash_and_is_skipped(
    patched: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-004-3: total_duration_min == None 이어도 크래시 없이 건너뛴다."""
    now = patched["now"]
    rows = [
        _row(1, now - timedelta(hours=2), None),  # 항목 없는 시술 → 스킵
        _row(2, now - timedelta(hours=3), 30),  # 경과 → 완료 대상
    ]
    monkeypatch.setattr(
        treatment_task,
        "get_treatments_to_autocomplete",
        lambda _db, _now: rows,
    )

    # float(None) 크래시가 나면 CustomException(500) 으로 감싸지므로
    # 예외 없이 통과해야 한다.
    treatment_task.auto_complete_treatment()

    assert patched["recorder"]["ids"] == [2]


def test_utc_naive_comparison_no_9h_skew(
    patched: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-004-4: reserved_at 과 UTC-naive 현재시각 비교로 9시간 오판정이 없다.

    reserved_at + duration 이 현재 시각보다 30분 앞선(=아직 미종료) 시술은
    완료되면 안 된다. 이전 KST-naive 비교(+9h)였다면 이 시술이 잘못 완료
    처리되었을 것이다.
    """
    now = patched["now"]
    # 종료 예정: now + 30분 (아직 미도래). KST 기준(+9h)으로 비교했다면 경과로 오판정됨.
    rows = [_row(1, now - timedelta(minutes=30), 60)]
    monkeypatch.setattr(
        treatment_task,
        "get_treatments_to_autocomplete",
        lambda _db, _now: rows,
    )

    treatment_task.auto_complete_treatment()

    # UTC 정합 비교이므로 완료 대상이 없어야 한다(.update 미호출).
    assert patched["recorder"].get("ids", []) == []
