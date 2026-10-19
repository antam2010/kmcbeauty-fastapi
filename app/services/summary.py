import logging
from calendar import monthrange
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import TypeVar

from sqlalchemy.orm import Session

from app.crud.statistics_crud import (
    get_staff_summary,
    get_today_reservation_list_with_customer_insight,
    get_treatment_sales_summary,
    get_treatment_summary,
)
from app.models.shop import Shop
from app.schemas.dashboard import (
    DashboardCustomerInsight,
    DashboardFilter,
    DashboardSalesSummary,
    DashboardStaffSummary,
    DashboardStaffSummaryItem,
    DashboardSummary,
    DashboardSummaryResponse,
    TreatmentSalesItem,
    TreatmentSummarySchema,
)
from app.utils.redis.dashboard import (
    clear_dashboard_cache,
    day_period,
    get_dashboard_cache,
    get_dashboard_cache_key,
    get_dashboard_cache_version,
    month_period,
    set_dashboard_cache,
)

T = TypeVar("T")


# 대시보드 집계 오케스트레이션: 캐시 키 정의·타입 디스패치·최종 조립이 한 흐름에
# 묶여 있어 분리 시 다수 지역 클로저(shop/db 캡처)와 캐시 경계가 흩어진다.
# 동작 보존 우선으로 복잡도 규칙을 억제한다.
def get_dashboard_summary_service(  # noqa: C901, PLR0915
    db: Session,
    shop: Shop,
    params: DashboardFilter,
) -> dict:
    target_date = params.target_date
    force_refresh = params.force_refresh

    month_start = target_date.replace(day=1)
    month_end = date(
        target_date.year,
        target_date.month,
        monthrange(target_date.year, target_date.month)[1],
    )

    # ---- 캐시 키 정의 ----
    # 기간 종류(day/month)를 키에 넣는다. 날짜만 쓰면 1일 조회 시 일 키와 월 키
    # (month_start)가 같아져 월간 통계에 하루치가 섞이거나 그 반대가 된다.
    # 캐시 버전은 요청당 한 번 읽는다. 예약 쓰기 커밋 후 버전이 오르면 이전 키는
    # 더 이상 읽히지 않는다(bump_dashboard_cache_version, TTL 로 만료).
    cache_version = get_dashboard_cache_version(shop.id)
    day = day_period(target_date)
    month = month_period(target_date)
    t_target_key = ("summary", day)
    t_month_key = ("summary", month)
    s_target_key = ("sales", day)
    s_month_key = ("sales", month)
    c_insight_key = ("customer_insight", day)
    staff_target_key = ("staff_summary", day)
    staff_month_key = ("staff_summary", month)

    # ---- 유틸: 이터러블/Row/직렬화 보조 ----
    def _is_iterable_but_not_str(x: object) -> bool:
        return isinstance(x, Iterable) and not isinstance(x, (str, bytes, bytearray))

    def _row_to_plain(x: object) -> object:
        """SQLAlchemy Row 지원: Row -> dict, 그 외는 그대로."""
        if hasattr(x, "_mapping"):  # sqlalchemy.engine.Row
            # SQLAlchemy Row 는 dict 변환용 공개 API 로 _mapping 을 노출한다.
            return dict(x._mapping)  # noqa: SLF001
        return x

    def _to_cacheable(obj: object) -> object:
        """캐시 저장용 변환: 모델이면 model_dump, Row면 dict, 리스트는 원소별 처리."""
        if isinstance(obj, list):
            out = []
            for it in obj:
                if hasattr(it, "model_dump"):
                    out.append(it.model_dump(mode="json"))
                else:
                    out.append(_row_to_plain(it))
            return out
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        return _row_to_plain(obj)

    # ---- 공통 캐시 처리 함수 ----
    # 반환형 확정(제너레이터/Row/tuple/Mapping 방어) 분기가 본질적으로 많아
    # 분리하면 오히려 타입 디스패치 흐름이 흩어진다. 동작 보존 우선으로 억제한다.
    def get_or_set_cache(  # noqa: C901, PLR0912
        key_tuple: tuple[str, str],
        get_func: Callable[[], list[T] | T],
        pydantic_model: type[T] | None = None,
        force_refresh: bool = False,
    ) -> list[T] | T:
        field, period = key_tuple
        cache_key = get_dashboard_cache_key(shop.id, cache_version, field, period)
        if force_refresh:
            clear_dashboard_cache(cache_key)

        cached = get_dashboard_cache(cache_key)
        if cached is not None and not force_refresh:
            # 캐시 히트: 모델로 복원
            if pydantic_model:
                if isinstance(cached, list):
                    logging.debug(
                        "Cache hit for %s on %s for shop %s - multiple items",
                        field,
                        period,
                        shop.id,
                    )
                    return [pydantic_model.model_validate(v) for v in cached]
                logging.debug(
                    "Cache hit for %s on %s for shop %s - single item",
                    field,
                    period,
                    shop.id,
                )
                return pydantic_model.model_validate(cached)
            logging.debug(
                "Cache hit for %s on %s for shop %s - raw data",
                field,
                period,
                shop.id,
            )
            return cached

        logging.debug(
            "Cache miss for %s on %s for shop %s, fetching data...",
            field,
            period,
            shop.id,
        )

        # 1) 원본 취득
        raw = get_func()

        # 2) 반환형 '확정' (제너레이터/Row/tuple(items) 등 방어)
        if pydantic_model:
            # (a) Mapping -> 단일 모델
            if isinstance(raw, Mapping):
                result_obj: list[T] | T = pydantic_model.model_validate(dict(raw))

            # (b) 이터러블 계열 -> 리스트로 확정
            elif _is_iterable_but_not_str(raw):
                seq = list(raw)  # 제너레이터 소모 방지

                # dict.items() 같은 (key, value) 튜플 나열이면 단일 dict로 합쳐서 모델
                if seq and all(
                    isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str)
                    for it in seq
                ):
                    result_obj = pydantic_model.model_validate(dict(seq))

                # Row/Mapping 들의 리스트면 각 원소를 dict로 정규화 후 모델 리스트
                elif seq and isinstance(_row_to_plain(seq[0]), Mapping):
                    result_obj = [
                        pydantic_model.model_validate(dict(_row_to_plain(it)))
                        for it in seq
                    ]

                # 그 외엔 요소별로 그대로 모델링 시도
                else:
                    result_obj = [
                        pydantic_model.model_validate(_row_to_plain(it)) for it in seq
                    ]

            # (c) 단일 Row/스칼라
            else:
                raw2 = _row_to_plain(raw)
                if isinstance(raw2, Mapping):
                    result_obj = pydantic_model.model_validate(dict(raw2))
                else:
                    result_obj = pydantic_model.model_validate(raw2)
        # pydantic_model 없을 때도 제너레이터/Row는 확정
        elif _is_iterable_but_not_str(raw):
            result_obj = list(raw)
        else:
            result_obj = _row_to_plain(raw)

        # 3) 캐시에 넣기 (항상 직렬화된 형태)
        to_cache = _to_cacheable(result_obj)
        set_dashboard_cache(cache_key, to_cache)

        return result_obj

    # ---- 실제 데이터 획득 ----
    treatment_target_summary = get_or_set_cache(
        t_target_key,
        lambda: get_treatment_summary(
            db,
            shop.id,
            start_date=target_date,
            end_date=target_date,
        ),
        pydantic_model=TreatmentSummarySchema,
        force_refresh=force_refresh,
    )

    treatment_month_summary = get_or_set_cache(
        t_month_key,
        lambda: get_treatment_summary(
            db,
            shop.id,
            start_date=month_start,
            end_date=month_end,
        ),
        pydantic_model=TreatmentSummarySchema,
        force_refresh=force_refresh,
    )

    treatment_sales_target = get_or_set_cache(
        s_target_key,
        lambda: get_treatment_sales_summary(
            db,
            shop.id,
            start_date=target_date,
            end_date=target_date,
        ),
        pydantic_model=TreatmentSalesItem,
        force_refresh=force_refresh,
    )

    treatment_sales_month = get_or_set_cache(
        s_month_key,
        lambda: get_treatment_sales_summary(
            db,
            shop.id,
            start_date=month_start,
            end_date=month_end,
        ),
        pydantic_model=TreatmentSalesItem,
        force_refresh=force_refresh,
    )

    customer_insight = get_or_set_cache(
        c_insight_key,
        lambda: get_today_reservation_list_with_customer_insight(
            db,
            shop.id,
            start_date=target_date,
            end_date=target_date,
        ),
        pydantic_model=DashboardCustomerInsight,
        force_refresh=force_refresh,
    )

    staff_target_summary = get_or_set_cache(
        staff_target_key,
        lambda: get_staff_summary(
            db,
            shop.id,
            start_date=target_date,
            end_date=target_date,
        ),
        pydantic_model=DashboardStaffSummaryItem,
        force_refresh=force_refresh,
    )

    staff_month_summary = get_or_set_cache(
        staff_month_key,
        lambda: get_staff_summary(
            db,
            shop.id,
            start_date=month_start,
            end_date=month_end,
        ),
        pydantic_model=DashboardStaffSummaryItem,
        force_refresh=force_refresh,
    )

    # ---- 최종 결과 조립 후 리턴 ----
    return DashboardSummaryResponse(
        target_date=target_date,
        summary=DashboardSummary(
            target_date=treatment_target_summary,
            month=treatment_month_summary,
        ),
        sales=DashboardSalesSummary(
            target_date=treatment_sales_target,
            month=treatment_sales_month,
        ),
        customer_insights=customer_insight,
        staff_summary=DashboardStaffSummary(
            target_date=staff_target_summary,
            month=staff_month_summary,
        ),
    ).model_dump()
