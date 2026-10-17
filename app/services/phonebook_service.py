import logging
from collections import defaultdict

from fastapi import status
from fastapi_pagination import Page
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.crud.phonebook_crud import (
    create_phonebook,
    get_all_phonebooks_by_shop,
    get_group_counts_by_groupname,
    get_phonebook_by_id,
    get_phonebook_by_phone_number,
    get_phonebooks_by_user,
    update_phonebook,
)
from app.exceptions import CustomException
from app.models.phonebook import Phonebook
from app.models.shop import Shop
from app.schemas.phonebook import (
    DuplicateCheckResponse,
    PhonebookCreate,
    PhonebookFilter,
    PhonebookGroupedByGroupnameResponse,
    PhonebookResponse,
    PhonebookUpdate,
)

# 전화번호부 관련 에러 도메인 상수
DOMAIN = "PHONEBOOK"


def get_phonebook_list_service(
    db: Session,
    params: PhonebookFilter,
    current_shop: Shop,
) -> Page[PhonebookResponse]:
    """전화번호부 목록 조회 서비스.

    Args:
        db: 데이터베이스 세션
        params: 필터링 파라미터 (검색어 등)
        current_shop: 현재 접속한 매장 정보

    Returns:
        Page[PhonebookResponse]: 페이지네이션된 전화번호부 목록

    """
    # 매장별 전화번호부 목록 조회 (검색 조건 포함)
    return get_phonebooks_by_user(db=db, shop_id=current_shop.id, search=params.search)


def get_phonebook_service(
    db: Session,
    current_shop: Shop,
    phonebook_id: int,
) -> PhonebookResponse:
    """전화번호부 상세 조회 서비스.

    Args:
        db: 데이터베이스 세션
        current_shop: 현재 접속한 매장 정보
        phonebook_id: 조회할 전화번호부 ID

    Returns:
        PhonebookResponse: 전화번호부 상세 정보

    Raises:
        CustomException: 전화번호부를 찾을 수 없을 때 404 에러

    """
    # 전화번호부 ID로 조회
    phonebook = get_phonebook_by_id(db, phonebook_id, current_shop.id)

    # 존재하지 않는 경우 404 에러
    if not phonebook:
        raise CustomException(status_code=status.HTTP_404_NOT_FOUND, domain=DOMAIN)

    return phonebook


def create_phonebook_service(
    db: Session,
    data: PhonebookCreate,
    current_shop: Shop,
) -> PhonebookResponse:
    """전화번호부 생성 서비스.

    Args:
        db: 데이터베이스 세션
        data: 생성할 전화번호부 정보
        current_shop: 현재 접속한 매장 정보

    Returns:
        PhonebookResponse: 생성된 전화번호부 정보

    Raises:
        CustomException:
            - 409: 전화번호 중복
            - 500: 데이터베이스 에러 또는 예상치 못한 에러

    """
    try:
        # 전화번호 중복 체크
        existing = get_phonebook_by_phone_number(db, data.phone_number, current_shop.id)
        if existing:
            raise CustomException(
                status_code=status.HTTP_409_CONFLICT,
                domain=DOMAIN,
                hint="전화번호 중복 확인하쇼.",
            )

        # 전화번호부 생성
        phonebook = create_phonebook(db, data, current_shop.id)
        db.commit()

    except CustomException:
        # CustomException은 그대로 전파
        db.rollback()
        raise
    except SQLAlchemyError as e:
        # 데이터베이스 관련 에러 처리
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e
    except Exception as e:
        # 그 외 예상치 못한 에러 처리
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e

    # 생성된 객체 새로고침하여 최신 정보 반환
    db.refresh(phonebook)
    return phonebook


def update_phonebook_service(
    db: Session,
    phonebook_id: int,
    data: PhonebookUpdate,
    current_shop: Shop,
) -> Phonebook:
    """전화번호부 수정 서비스.

    Args:
        db: 데이터베이스 세션
        phonebook_id: 수정할 전화번호부 ID
        data: 수정할 정보
        current_shop: 현재 접속한 매장 정보

    Returns:
        Phonebook: 수정된 전화번호부 정보

    Raises:
        CustomException:
            - 404: 전화번호부를 찾을 수 없음
            - 409: 변경하려는 전화번호가 이미 존재함
            - 500: 데이터베이스 에러 또는 예상치 못한 에러

    """
    # 변경하려는 전화번호가 다른 레코드에 존재하는지 확인
    existing = get_phonebook_by_phone_number(db, data.phone_number, current_shop.id)
    if existing and existing.id != phonebook_id:
        raise CustomException(status_code=status.HTTP_409_CONFLICT, domain=DOMAIN)

    # 수정할 전화번호부 조회
    phonebook = get_phonebook_by_id(db, phonebook_id, current_shop.id)
    if not phonebook:
        raise CustomException(status_code=status.HTTP_404_NOT_FOUND, domain=DOMAIN)

    try:
        # 전화번호부 정보 업데이트
        update_phonebook(db, phonebook, data)
        db.commit()
    except SQLAlchemyError as e:
        # 데이터베이스 관련 에러 처리
        db.rollback()
        logging.exception("SQLAlchemyError")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e
    except Exception as e:
        # 그 외 예상치 못한 에러 처리
        db.rollback()
        logging.exception("Unexpected error")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e

    # 수정된 객체 새로고침하여 최신 정보 반환
    db.refresh(phonebook)
    return phonebook


def delete_phonebook_service(
    db: Session,
    phonebook_id: int,
    current_shop: Shop,
) -> None:
    """전화번호부 삭제 서비스 (소프트 삭제).

    Args:
        db: 데이터베이스 세션
        phonebook_id: 삭제할 전화번호부 ID
        current_shop: 현재 접속한 매장 정보

    Raises:
        CustomException:
            - 404: 전화번호부를 찾을 수 없음
            - 500: 데이터베이스 에러 또는 예상치 못한 에러

    """
    # 삭제할 전화번호부 조회
    phonebook = get_phonebook_by_id(db, phonebook_id, current_shop.id)
    if not phonebook:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
        )

    try:
        # 소프트 삭제 처리 (deleted_at 필드 업데이트)
        Phonebook.soft_delete(phonebook)
        db.commit()
    except SQLAlchemyError as e:
        # 데이터베이스 관련 에러 처리
        db.rollback()
        logging.exception("SQLAlchemyError occurred while deleting phonebook")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e
    except Exception as e:
        # 그 외 예상치 못한 에러 처리
        db.rollback()
        logging.exception("Unexpected error occurred while deleting phonebook")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e


def get_grouped_by_groupname_service(
    db: Session,
    current_shop: Shop,
    with_items: bool,
) -> list[PhonebookGroupedByGroupnameResponse]:
    """그룹명으로 그룹화된 전화번호부 조회 서비스.

    Args:
        db: 데이터베이스 세션
        current_shop: 현재 접속한 매장 정보
        with_items: True일 경우 각 그룹의 전화번호부 목록도 함께 반환

    Returns:
        list[PhonebookGroupedByGroupnameResponse]: 그룹별 전화번호부 정보
            - group_name: 그룹명
            - count: 해당 그룹의 전화번호부 개수
            - items: 해당 그룹의 전화번호부 목록 (with_items=True일 때만)

    """
    # 그룹별 전화번호부 개수 조회
    group_data = get_group_counts_by_groupname(db, current_shop.id)

    # 그룹별 전화번호부 목록을 저장할 딕셔너리
    group_map: dict[str | None, list[PhonebookResponse]] = defaultdict(list)

    # with_items가 True일 경우에만 전체 전화번호부 조회
    if with_items:
        # 매장의 모든 전화번호부 조회 (삭제된 것 제외)
        all_items = get_all_phonebooks_by_shop(db, current_shop.id)

        # 그룹별로 전화번호부 분류
        for item in all_items:
            key = item.group_name
            group_map[key].append(PhonebookResponse.model_validate(item))

    # 결과 생성
    result = []
    for group_name, count in group_data:
        # with_items가 True일 때만 items 포함, 아니면 빈 리스트
        items = group_map[group_name] if with_items else []

        result.append(
            PhonebookGroupedByGroupnameResponse(
                group_name=group_name,
                count=count,
                items=items,
            ),
        )

    return result


def check_duplicate_phone_number_service(
    db: Session,
    current_shop: Shop,
    phone_number: str,
) -> DuplicateCheckResponse:
    """전화번호 중복 체크 서비스.

    Args:
        db: 데이터베이스 세션
        current_shop: 현재 접속한 매장 정보
        phone_number: 중복 체크할 전화번호. 라우터가 생성 경로와 같은 규칙으로 검증 ·
            정규화한 값(000-0000-0000)이라 저장값과 그대로 비교한다.

    Returns:
        DuplicateCheckResponse: 중복 여부 및 정규화된 전화번호

    """
    # 해당 전화번호가 이미 존재하는지 확인
    try:
        existing = get_phonebook_by_phone_number(db, phone_number, current_shop.id)
        exists = existing is not None
    except SQLAlchemyError as e:
        logging.exception("SQLAlchemyError during duplicate check")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e
    except Exception as e:
        logging.exception("Unexpected error during duplicate check")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e
    return DuplicateCheckResponse(
        exists=exists,
        phone_number=phone_number,
    )
