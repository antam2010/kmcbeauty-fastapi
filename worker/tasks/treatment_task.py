from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.crud.treatment_crud import (
    AUTOCOMPLETE_BATCH_SIZE,
    get_treatments_to_autocomplete,
)
from app.database import SessionLocal
from app.enum.treatment_status import TreatmentStatus
from app.exceptions import CustomException
from app.models.treatment import Treatment
from app.schemas.treatment import TreatmentAutoComplete
from app.utils.datetime import now_utc
from app.utils.redis.dashboard import bump_dashboard_cache_version
from celery_app import celery_app

DOMAIN = "treatment_task"

# 실행 1회당 최대 배치 수(무한 루프 방지 상한). 배치 100건 x 50 = 실행당 최대
# 5,000건(beat 30분 주기)이며, 남은 대상은 다음 실행이 이어서 처리한다.
MAX_BATCHES_PER_RUN = 50


def _is_elapsed(row: TreatmentAutoComplete, now: datetime) -> bool:
    """종료 시각(reserved_at + 소요시간 합)이 now 이하인지 확인한다.

    SQL 이 이미 거른 행이지만 잘못 조회된 행이 먼저 완료되지 않도록 한 번 더 본다.
    datetime + timedelta 는 큰 소요시간에서 OverflowError 를 내 배치 전체를 실패시키므로
    경과 초와 소요 초를 숫자로 비교한다. 항목 없는 시술(None)은 건너뛴다.
    """
    if row.total_duration_min is None:
        return False
    elapsed_seconds = (now - row.reserved_at).total_seconds()
    return elapsed_seconds >= float(row.total_duration_min) * 60


# @MX:WARN: [AUTO] 자동완료 판정은 타임존/NULL/동시성 경계와 대상 상태 정책에 민감하다.
# @MX:REASON: (1) reserved_at 는 naive DateTime 컬럼이며 저장 규약이 UTC(앱이 UTC ISO
#             전송, 서버 TimestampMixin/SoftDeleteMixin 모두 UTC 사용)이므로 비교 기준을
#             반드시 UTC-naive 로 맞춰야 한다. 이전 now_kst().replace(tzinfo=None) 는
#             KST-naive 로 9시간 어긋나 오판정을 유발했다(SPEC-FIX-001 REQ-FIX-004).
#             (2) 항목 없는 시술은 total_duration_min(SUM)이 None 이라 건너뛴다.
#             (3) 대상은 직원이 방문 처리한 VISITED 뿐이다(autocomplete_statuses).
#             RESERVED 를 완료하면 노쇼일 수 있는 예약이 결제 수단 기본값(CARD)으로
#             실매출에 잡히고 노쇼로 집계되지 않는다. RESERVED 는 직원이 방문/노쇼를
#             고른다.
#             (4) 조회와 UPDATE 사이에 VISITED 가 아니게 된(취소·노쇼·예약으로 되돌림)
#             예약을 덮어쓰지 않도록 UPDATE 가 같은 대상 상태와 finished_at IS NULL
#             을 다시 확인한다.
@celery_app.task
def auto_complete_treatment() -> None:
    """종료 시각이 지난 방문(VISITED) 예약을 COMPLETED 로 바꾼다(beat 30분 주기).

    RESERVED 는 대상이 아니다. 종료 시각이 지나도 RESERVED 로 남고 직원이 방문/노쇼를
    직접 고른다.
    """
    db: Session = SessionLocal()
    try:
        # reserved_at 는 UTC-naive 로 저장되므로 비교 기준도 UTC-naive 로 정규화한다.
        now = now_utc().replace(tzinfo=None)
        target_statuses = TreatmentStatus.autocomplete_statuses()

        # 끝난 예약이 남지 않도록 배치가 빌 때까지 반복한다(상한 있음).
        for _ in range(MAX_BATCHES_PER_RUN):
            rows = get_treatments_to_autocomplete(db, now)
            complete_ids = [row.treatment_id for row in rows if _is_elapsed(row, now)]

            if complete_ids:
                # synchronize_session=False 중요, ORM 상태 추적없이 곧바로 sql 만 실행
                db.query(Treatment).filter(
                    Treatment.id.in_(complete_ids),
                    # 조회 이후 VISITED 가 아니게 된 예약(취소·노쇼·완료·예약으로
                    # 되돌림)은 덮어쓰지 않는다.
                    Treatment.status.in_(target_statuses),
                    Treatment.finished_at.is_(None),
                ).update(
                    {
                        Treatment.status: "COMPLETED",
                        Treatment.finished_at: now,
                    },
                    synchronize_session=False,
                )

            db.commit()
            # 커밋이 성공한 뒤에만, 완료 처리한 예약이 속한 상점의 대시보드 캐시를
            # 무효화한다(배치마다: 뒤 배치가 실패해도 앞 배치 반영분은 보인다).
            completed = set(complete_ids)
            for shop_id in {r.shop_id for r in rows if r.treatment_id in completed}:
                bump_dashboard_cache_version(shop_id)

            # 마지막 배치이거나 진전이 없으면(같은 행 재조회 방지) 멈춘다.
            if len(rows) < AUTOCOMPLETE_BATCH_SIZE or not complete_ids:
                break
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=500,
            domain=DOMAIN,
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=500,
            domain=DOMAIN,
            exception=e,
        ) from e
    finally:
        db.close()
