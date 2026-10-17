from fastapi import APIRouter, Body, Depends, Request, status
from fastapi_pagination import Page
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.shop import Shop
from app.models.user import User
from app.schemas.shop import ShopCreate, ShopResponse, ShopSelect, ShopUpdate
from app.schemas.shop_invite import ShopInviteCreateRequest, ShopInviteResponse
from app.schemas.shop_user import (
    ShopUserAssociateRequest,
    ShopUserAssociateUpdateRequest,
    ShopUserUserResponse,
)
from app.services.shop_invite_service import (
    delete_invite_code_service,
    generate_invite_code_service,
    get_invite_code_service,
)
from app.services.shop_service import (
    delete_selected_shop_service,
    get_my_shops_service,
    get_selected_shop_service,
    set_selected_shop_service,
    upsert_shop_service,
)
from app.services.shop_user_service import (
    create_shop_user_service,
    delete_shop_user_service,
    get_shop_users_service,
    update_shop_user_service,
)

router = APIRouter(prefix="/shops", tags=["상점"])


@router.get(
    "",
    response_model=Page[ShopResponse],
    summary="내 샵 목록 조회",
    description="로그인한 유저의 샵 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def get_my_shops(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Page[ShopResponse]:
    return get_my_shops_service(db=db, user=current_user)


@router.post(
    "",
    response_model=ShopResponse,
    summary="샵 생성",
    description="새로운 샵을 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
    },
)
def create_shop(
    shop_data: ShopCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShopResponse:
    return upsert_shop_service(db=db, user=current_user, shop_data=shop_data)


@router.put(
    "/{shop_id}",
    response_model=ShopResponse,
    summary="샵 수정",
    description="샵 정보를 수정합니다.",
    status_code=status.HTTP_200_OK,
)
def update_shop(
    shop_id: int,
    shop_data: ShopUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShopResponse:
    return upsert_shop_service(
        db=db,
        user=current_user,
        shop_data=shop_data,
        shop_id=shop_id,
    )


@router.post(
    "/selected",
    summary="선택한 샵 설정",
    description="현재 선택한 샵을 설정합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def select_shop(
    params: ShopSelect,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    return set_selected_shop_service(db=db, user=current_user, shop_id=params.shop_id)


@router.get(
    "/selected",
    response_model=ShopResponse,
    summary="선택한 샵 조회",
    description="현재 선택된 샵을 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def get_selected_shop(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Shop:
    return get_selected_shop_service(db=db, user=current_user)


@router.delete(
    "/selected",
    summary="선택한 샵 삭제",
    description="현재 선택된 샵을 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def delete_selected_shop(
    db: Session = Depends(get_db),  # noqa: ARG001  # 요청당 DB 세션 수명주기 유지를 위해 주입을 보존한다
    current_user: User = Depends(get_current_user),
) -> None:
    return delete_selected_shop_service(user=current_user)


@router.post(
    "/{shop_id}/invites",
    response_model=ShopInviteResponse,
    summary="샵 초대코드 생성",
    description=(
        "샵에 초대코드를 생성합니다. 만료 기간은 JSON 본문의 expire_in(초, 60초 이상 "
        "30일 이하)으로 정하며, 본문이 없거나 {} 이면 7일입니다."
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
# 초대코드 생성 남용 방지: IP 당 분당 10회(slowapi 키 스타일이 url 이라 샵별로 센다)
@limiter.limit("10/minute")
def create_invite_link(
    request: Request,  # noqa: ARG001  # slowapi limiter 가 IP 추출을 위해 request 를 요구한다
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    # 앱은 {"expire_in": 초} 를 JSON 본문으로 보낸다(쿼리 아님). Depends() 로 받으면
    # 쿼리로만 읽혀 본문 값이 무시되고 늘 7일이었다. 본문이 없거나 {} 이면 기본값이다.
    invite_data: ShopInviteCreateRequest = Body(default=ShopInviteCreateRequest()),
) -> ShopInviteResponse:
    return generate_invite_code_service(
        db=db,
        shop_id=shop_id,
        user=current_user,
        invite_data=invite_data,
    )


@router.get(
    "/{shop_id}/invites",
    response_model=ShopInviteResponse,
    summary="샵 초대코드 조회",
    description="샵에 초대코드를 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def get_invite_link(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShopInviteResponse:
    return get_invite_code_service(db=db, shop_id=shop_id, user=current_user)


@router.delete(
    "/{shop_id}/invites",
    summary="샵 초대코드 삭제",
    description="샵에 초대코드를 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def delete_invite_link(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    return delete_invite_code_service(db=db, shop_id=shop_id, user=current_user)


@router.get(
    "/{shop_id}/users",
    response_model=list[ShopUserUserResponse],
    summary="샵 유저 목록 조회",
    description="특정 샵에 속한 유저 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def get_shop_users(
    shop_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ShopUserUserResponse]:
    return get_shop_users_service(db=db, shop_id=shop_id, current_user=current_user)


# @MX:ANCHOR: [AUTO] 외부 앱이 의존하는 직원(샵 유저) 쓰기 계약 경계.
# @MX:REASON: app/api/services/staff.ts 의 createUser/updateUser/deleteUser 가
#             호출하는 POST/PUT/DELETE /shops/{shop_id}/users[/{user_id}] 진입점.
#             인가는 서비스의 _require_primary_owner(멤버십+대표원장) 로 강제하며
#             role 등 권한 상승 필드는 계약에 포함하지 않는다(SPEC-SECURITY-001).
@router.post(
    "/{shop_id}/users",
    response_model=ShopUserUserResponse,
    summary="샵 유저 연결",
    description=(
        "기존 유저(email)를 샵에 연결(멤버십 부여)합니다. 대표원장만 가능. "
        "유저 계정 생성/role 결정은 하지 않습니다(SECURITY-001 소관)."
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
def create_shop_user(
    shop_id: int,
    payload: ShopUserAssociateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShopUserUserResponse:
    return create_shop_user_service(
        db=db,
        shop_id=shop_id,
        current_user=current_user,
        payload=payload,
    )


@router.put(
    "/{shop_id}/users/{user_id}",
    response_model=ShopUserUserResponse,
    summary="샵 유저 연결 수정",
    description=(
        "샵-유저 연결(대표원장 여부)을 수정합니다. 대표원장만 가능. "
        "role 등 권한 필드는 다루지 않습니다(SECURITY-001 소관)."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def update_shop_user(
    shop_id: int,
    user_id: int,
    payload: ShopUserAssociateUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShopUserUserResponse:
    return update_shop_user_service(
        db=db,
        shop_id=shop_id,
        user_id=user_id,
        current_user=current_user,
        payload=payload,
    )


@router.delete(
    "/{shop_id}/users/{user_id}",
    response_model=None,
    summary="샵 유저 연결 삭제",
    description=(
        "샵-유저 연결(멤버십)을 해제합니다. 대표원장만 가능. 연결 테이블 매핑만 "
        "제거하며 유저 계정은 유지됩니다."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def delete_shop_user(
    shop_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    return delete_shop_user_service(
        db=db,
        shop_id=shop_id,
        user_id=user_id,
        current_user=current_user,
    )
