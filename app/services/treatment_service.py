from fastapi import status
from fastapi_pagination import Page
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.crud.phonebook_crud import get_phonebook_by_id
from app.crud.shop_user_crud import get_shop_user
from app.crud.treatment_crud import (
    create_treatment,
    get_treatment_by_id,
    get_treatment_items_by_treatment_id,
    get_treatment_list,
    validate_menu_detail_exists,
)
from app.enum.treatment_status import TreatmentStatus
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.user import User
from app.schemas.treatment import (
    TreatmentCreate,
    TreatmentFilter,
    TreatmentResponse,
    TreatmentSimpleResponse,
    TreatmentUpdate,
)
from app.schemas.treatment_item import TreatmentItemCreate

DOMAIN = "TREATMENT"


def get_treatment_list_service(
    db: Session,
    current_shop: Shop,
    filters: TreatmentFilter,
) -> Page[TreatmentResponse]:
    """시술 예약 목록을 조회하는 서비스.

    :param db: DB 세션
    :param current_shop: 현재 상점
    :param filters: TreatmentFilter 모델
    :return: Treatment 모델
    """
    try:
        return get_treatment_list(
            db=db,
            shop_id=current_shop.id,
            filters=filters,
        )

    except SQLAlchemyError as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="Unknown Error",
            exception=e,
        ) from e


def upsert_treatment_service(
    db: Session,
    data: TreatmentCreate | TreatmentUpdate,
    current_shop: Shop,
    current_user: User,
    treatment_id: int | None = None,
) -> TreatmentSimpleResponse:
    """시술 예약을 생성하거나 업데이트하는 서비스.

    :param db: DB 세션
    :param data: TreatmentCreate 또는 TreatmentUpdate 모델
    :param current_shop: 현재 상점
    :param current_user: 요청자. 생성 시 created_user_id 로 기록한다(수정은 불변).
    :param treatment_id: 시술 예약 ID (업데이트 시 필요)
    :return: TreatmentResponse 모델
    """
    try:
        if treatment_id is None:
            treatment = _create_treatment(db, data, current_shop, current_user)
        else:
            treatment = _update_treatment(db, data, current_shop, treatment_id)

        # 수정에서 항목이 생략/null(None)이면 기존 항목을 건드리지 않는다.
        # 생성(TreatmentCreate)은 기본값이 빈 목록이라 항상 이 경로를 탄다.
        if data.treatment_items is not None:
            _upsert_treatment_items(
                db,
                treatment.id,
                current_shop.id,
                data.treatment_items,
            )

        db.commit()
        db.refresh(treatment)
        return TreatmentSimpleResponse.model_validate(treatment)

    except CustomException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


# @MX:ANCHOR: [AUTO] 외부 앱(kmcbeauty-native)이 의존하는 예약 삭제 계약 경계.
# @MX:REASON: app/api/services/treatment.ts::remove 가 호출하는 DELETE /treatments/{id}
#             의 백엔드 진입점. shop 소유권(get_current_shop, SECURITY-001)과
#             삭제 시맨틱(상태 CANCELLED 전환)을 이 지점에서 강제한다.
def cancel_treatment_service(
    db: Session,
    current_shop: Shop,
    treatment_id: int,
) -> None:
    """시술 예약 삭제 서비스(상태 전환 방식의 논리 삭제).

    Treatment 모델은 SoftDeleteMixin(deleted_at)을 갖지 않으므로(SPEC-FIX-001에서
    Phonebook/Shop/TreatmentMenu(+Detail)/User 에만 적용) 물리적 소프트삭제 대신
    예약 상태를 CANCELLED 로 전환해 이력을 보존한다. 물리 삭제는 예약/시술항목
    이력을 파기하므로 채택하지 않는다.

    :param db: DB 세션
    :param current_shop: 현재 상점(get_current_shop 로 소유권 검증됨)
    :param treatment_id: 삭제할 시술 예약 ID
    :raises CustomException: 404(예약 없음/타 상점 예약), 500(DB/알 수 없는 오류)
    """
    treatment = get_treatment_by_id(db, treatment_id)
    # shop 소유권 검증: 다른 상점의 예약은 조회되더라도 삭제 불가(BOLA 방지).
    if not treatment or treatment.shop_id != current_shop.id:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            detail="시술 예약을 찾을 수 없습니다.",
        )

    # 멱등 처리: 이미 CANCELLED 인 예약은 재삭제 시에도 성공(204)으로 처리한다.
    if treatment.status == TreatmentStatus.CANCELLED:
        return

    try:
        treatment.status = TreatmentStatus.CANCELLED
        db.commit()
    except CustomException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


# 참조 검증 실패 메시지(필드별 단일). 없음/타 샵/삭제됨을 구분하지 않는다.
_INVALID_PHONEBOOK = "고객 정보(phonebook_id)가 유효하지 않습니다."
_INVALID_STAFF_USER = "시술 담당자(staff_user_id)가 유효하지 않습니다."
_INVALID_MENU_DETAIL = "시술 항목(menu_detail_id)이 유효하지 않습니다."


# @MX:NOTE: [AUTO] 교차 테넌트 FK 방지 — 클라이언트가 보낸 참조 id
#            (phonebook_id / staff_user_id / menu_detail_id)는 현재 샵 소속일
#            때만 저장한다. GET /treatments 가 phonebook 과 staff_user 를
#            joinedload 하므로 미검증 참조는 타 샵 고객정보와 유저 이메일을
#            유출한다. 실패는 필드별 단일 400 이라 존재 여부 오라클이 없다.
def _invalid_reference(detail: str) -> CustomException:
    return CustomException(
        status_code=status.HTTP_400_BAD_REQUEST,
        domain=DOMAIN,
        detail=detail,
        hint="현재 상점에 속한 유효한 대상만 지정할 수 있습니다.",
    )


def _ensure_phonebook_in_shop(db: Session, shop_id: int, phonebook_id: int) -> None:
    # get_phonebook_by_id 는 shop_id 와 소프트삭제 제외를 함께 필터한다.
    if get_phonebook_by_id(db, phonebook_id, shop_id) is None:
        raise _invalid_reference(_INVALID_PHONEBOOK)


def _ensure_staff_in_shop(db: Session, shop_id: int, staff_user_id: int) -> None:
    # 시술 담당자는 현재 샵 멤버(shop_user 행)여야 한다.
    if get_shop_user(db, shop_id, staff_user_id) is None:
        raise _invalid_reference(_INVALID_STAFF_USER)


def _create_treatment(
    db: Session,
    data: TreatmentCreate,
    shop: Shop,
    current_user: User,
) -> Treatment:
    # 생성 시 고객/담당자 참조는 INSERT 이전에 검증한다. 시술 항목은
    # _upsert_treatment_items 가 항목 쓰기 전에 검증하고, 실패하면 서비스가 롤백한다.
    if data.phonebook_id is not None:
        _ensure_phonebook_in_shop(db, shop.id, data.phonebook_id)
    if data.staff_user_id is not None:
        _ensure_staff_in_shop(db, shop.id, data.staff_user_id)

    create_data = data.model_dump(exclude={"treatment_items"})
    create_data["shop_id"] = shop.id
    # 생성자는 요청자다(상점 원장 shop.user_id 가 아님 — 직원이 만든 예약 포함).
    create_data["created_user_id"] = current_user.id
    treatment = Treatment(**create_data)
    return create_treatment(db, treatment)


def _update_treatment(
    db: Session,
    data: TreatmentUpdate,
    shop: Shop,
    treatment_id: int,
) -> Treatment:
    treatment = get_treatment_by_id(db, treatment_id)
    if not treatment or treatment.shop_id != shop.id:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            detail="시술 예약을 찾을 수 없습니다.",
        )
    update_data = data.model_dump(exclude={"treatment_items"}, exclude_unset=True)

    # 기존 예약 편집 비파괴: 값이 지정되고(None 아님) 현재 값과 다를 때만 검증한다.
    # 이후 소프트삭제된 고객이나 샵을 떠난 담당자를 그대로 유지하는 수정은 허용한다.
    phonebook_id = update_data.get("phonebook_id")
    if phonebook_id is not None and phonebook_id != treatment.phonebook_id:
        _ensure_phonebook_in_shop(db, shop.id, phonebook_id)
    staff_user_id = update_data.get("staff_user_id")
    if staff_user_id is not None and staff_user_id != treatment.staff_user_id:
        _ensure_staff_in_shop(db, shop.id, staff_user_id)

    for key, value in update_data.items():
        setattr(treatment, key, value)
    return treatment


def _upsert_treatment_items(
    db: Session,
    treatment_id: int,
    shop_id: int,
    items: list[TreatmentItemCreate],
) -> None:
    existing_items = get_treatment_items_by_treatment_id(db, treatment_id)
    existing_items_map = {item.id: item for item in existing_items}
    received_ids = set()

    # 이 예약이 이미 참조 중인 상세 ID 는 신규 참조가 아니다. 이후 소프트삭제되었어도
    # 유지할 수 있다(클라이언트가 수정 시 항목 id 없이 전체를 재전송하는 경우 포함).
    referenced_detail_ids = {item.menu_detail_id for item in existing_items}

    # 쓰기 전에 모든 항목을 검증한다.
    # 샵 소유권은 항상 검사하고, 삭제 상태는 신규 참조만 검사한다.
    for item in items:
        if item.menu_detail_id is None or not validate_menu_detail_exists(
            db,
            item.menu_detail_id,
            shop_id,
            exclude_deleted=item.menu_detail_id not in referenced_detail_ids,
        ):
            raise _invalid_reference(_INVALID_MENU_DETAIL)

    for item in items:
        if getattr(item, "id", None) and item.id in existing_items_map:
            treatment_item = existing_items_map[item.id]
            treatment_item.menu_detail_id = item.menu_detail_id
            treatment_item.base_price = item.base_price
            treatment_item.duration_min = item.duration_min
            treatment_item.session_no = item.session_no
            received_ids.add(item.id)
        else:
            db.add(
                TreatmentItem(
                    treatment_id=treatment_id,
                    menu_detail_id=item.menu_detail_id,
                    base_price=item.base_price,
                    duration_min=item.duration_min,
                    session_no=item.session_no,
                ),
            )

    for item_id, item in existing_items_map.items():
        if item_id not in received_ids:
            db.delete(item)
