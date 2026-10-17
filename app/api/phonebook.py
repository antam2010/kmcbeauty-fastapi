from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi_pagination import Page
from pydantic import AfterValidator
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.shop import get_current_shop
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.shop import Shop
from app.schemas.phonebook import (
    DuplicateCheckResponse,
    PhonebookCreate,
    PhonebookFilter,
    PhonebookGroupedByGroupnameResponse,
    PhonebookResponse,
    PhonebookUpdate,
    validate_korean_phone_number,
)
from app.services.phonebook_service import (
    check_duplicate_phone_number_service,
    create_phonebook_service,
    delete_phonebook_service,
    get_grouped_by_groupname_service,
    get_phonebook_list_service,
    get_phonebook_service,
    update_phonebook_service,
)

router = APIRouter(prefix="/phonebooks", tags=["전화번호부"])


# 전화번호부 목록 조회
@router.get(
    "",
    response_model=Page[PhonebookResponse],
    summary="전화번호부 목록 조회",
    description="전화번호부 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def list_phonebook(
    params: PhonebookFilter = Depends(),
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> Page[PhonebookResponse]:
    return get_phonebook_list_service(
        db,
        params=params,
        current_shop=current_shop,
    )


# 전화번호부 그룹 목록
@router.get(
    "/groups",
    response_model=list[PhonebookGroupedByGroupnameResponse],
    summary="전화번호부 그룹 목록 조회",
    description="전화번호부 그룹 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def list_groups_by_group_name(
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
    with_items: bool = Query(False, description="전화번호부 항목 포함 여부"),
) -> list[PhonebookGroupedByGroupnameResponse]:
    return get_grouped_by_groupname_service(
        db,
        with_items=with_items,
        current_shop=current_shop,
    )


# 이미 전화번호부에 등록된 번호인지 확인
@router.get(
    "/check-duplicate",
    status_code=status.HTTP_200_OK,
    response_model=DuplicateCheckResponse,
    summary="전화번호부 중복 확인",
    description="이미 전화번호부에 등록된 번호인지 확인합니다.",
)
def check_duplicate_phone_number(
    # 생성 경로(PhonebookCreate)와 같은 규칙(20자 · 검증 · 정규화)을 조회 전에 적용한다.
    # 잘못된 번호는 422, 01012345678 처럼 정규화 전 번호도 저장된 정규형으로 찾는다.
    # `= Query(...)` 기본값 방식은 AfterValidator 를 적용하지 않아 Annotated 로 쓴다.
    phone_number: Annotated[
        str,
        Query(max_length=20, description="확인할 전화번호"),
        AfterValidator(validate_korean_phone_number),
    ],
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> DuplicateCheckResponse:
    return check_duplicate_phone_number_service(db, current_shop, phone_number)


# 전화번호부 상세 조회
@router.get(
    "/{phonebook_id}",
    response_model=PhonebookResponse,
    summary="전화번호부 상세 조회",
    description="전화번호부 항목을 상세 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def read_phonebook_handler(
    phonebook_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> PhonebookResponse:
    return get_phonebook_service(db, current_shop, phonebook_id)


# 전화번호부 생성
@router.post(
    "",
    response_model=PhonebookResponse,
    summary="전화번호부 생성",
    description="새로운 전화번호부 항목을 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
def create_phonebook_handler(
    phonebook: PhonebookCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> PhonebookResponse:
    return create_phonebook_service(db, phonebook, current_shop)


# 전화번호부 수정
@router.put(
    "/{phonebook_id}",
    response_model=PhonebookResponse,
    summary="전화번호부 수정",
    description="전화번호부 항목을 수정합니다.",
    status_code=status.HTTP_200_OK,
)
def update_phonebook_handler(
    phonebook_id: int,
    phonebook: PhonebookUpdate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> PhonebookResponse:
    return update_phonebook_service(db, phonebook_id, phonebook, current_shop)


# 전화번호부 삭제
@router.delete(
    "/{phonebook_id}",
    status_code=204,
    summary="전화번호부 삭제",
    description="전화번호부 항목을 소프트 삭제합니다.",
)
def delete_phonebook_handler(
    phonebook_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> None:
    delete_phonebook_service(db, phonebook_id, current_shop)
