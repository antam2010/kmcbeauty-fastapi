from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import EmailStr
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.user import User
from app.schemas.user import (
    UserCreate,
    UserEmailCheckResponse,
    UserResponse,
    UserUpdate,
)
from app.services.user_service import (
    check_user_email_service,
    create_user_service,
    delete_user_service,
    get_user_service,
    update_user_service,
)

# 사용자 관련 API 그룹 지
router = APIRouter(prefix="/users", tags=["유저"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="사용자 조회",
    description="현재 로그인한 사용자의 정보를 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def read_user_handler(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    return get_user_service(db, current_user)


@router.post(
    "",
    response_model=UserResponse,
    summary="사용자 생성",
    description="새로운 사용자를 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
# 가입 남용 · 초대코드 대입 방지: IP 당 분당 5회
@limiter.limit("5/minute")
def create_user_handler(
    request: Request,  # noqa: ARG001  # slowapi limiter 가 IP 추출을 위해 request 를 요구한다
    user: UserCreate,
    db: Session = Depends(get_db),
) -> UserResponse:
    return create_user_service(db, user)


@router.put(
    "/me",
    response_model=UserResponse,
    summary="사용자 수정",
    description="사용자 정보를 수정합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
def update_user_handler(
    user: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    return update_user_service(db, user, current_user)


@router.delete(
    "/me",
    summary="사용자 삭제",
    description="현재 로그인한 사용자를 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def delete_user_handler(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """현재 로그인한 사용자를 삭제합니다."""
    delete_user_service(db=db, current_user=current_user, is_soft_delete=True)


@router.delete(
    "/me/force",
    summary="사용자 강제 삭제",
    description="현재 로그인한 사용자를 강제로 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def delete_user_force_handler(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """현재 로그인한 사용자를 강제로 삭제합니다."""
    delete_user_service(db=db, current_user=current_user, is_soft_delete=False)


@router.get(
    "/exists/email",
    response_model=UserEmailCheckResponse,
    summary="이메일 중복 체크",
    description="이메일 중복 체크 API",
    status_code=status.HTTP_200_OK,
)
# 가입 여부(계정) 열거 방지: IP 당 분당 20회
@limiter.limit("20/minute")
def check_user_exsist(
    request: Request,  # noqa: ARG001  # slowapi limiter 가 IP 추출을 위해 request 를 요구한다
    email: EmailStr = Query(..., description="이메일 주소"),
    db: Session = Depends(get_db),
) -> UserEmailCheckResponse:
    return check_user_email_service(db, email=email)
