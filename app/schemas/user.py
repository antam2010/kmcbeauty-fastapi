from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, EmailStr, Field, model_validator

from app.enum.role import UserRole
from app.schemas.mixin.base import BaseResponseModel
from app.utils.email import normalize_email

# 저장·식별용 이메일 타입: EmailStr 형식 검증 후 소문자 정규형으로 바꾼다.
# EmailStr 은 도메인만 소문자화하고 로컬 파트 대소문자는 보존하므로, 대소문자를
# 구분하는 PostgreSQL 에서 대소문자만 다른 중복 가입을 막으려면 정규화가 필요하다.
NormalizedEmailStr = Annotated[EmailStr, AfterValidator(normalize_email)]


class UserBase(BaseResponseModel):
    name: str = Field(..., min_length=3, max_length=50, description="이름")
    email: NormalizedEmailStr = Field(..., description="유효한 이메일 주소")


class UserRoleMixin(BaseResponseModel):
    """서버가 결정한 유저 권한을 응답에 노출하기 위한 믹스인.

    권한 상승 방지: role 은 클라이언트 입력(UserCreate)이 아니라 서버가 결정한다.
    응답 스키마에서만 role 을 노출한다.
    """

    role: UserRole = Field(..., description="유저 권한")


class UserBaseResponse(UserBase, UserRoleMixin):
    """유저 기본 응답 스키마."""

    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }


class UserCreate(UserBase):
    """유저 생성 요청 스키마.

    권한 상승 방지: 클라이언트는 role 을 전송할 수 없다. 서버가 invite_code 유무로
    role 을 결정한다(초대 코드 있으면 MANAGER, 없으면 기본값 MASTER).
    """

    password: str = Field(..., min_length=4, description="비밀번호")
    invite_code: str | None = Field(
        None,
        description="초대 코드",
        min_length=10,
        max_length=11,
    )


class UserUpdate(UserBase):
    """유저 수정 요청 스키마.

    권한 상승 방지: role 등 권한 필드는 포함하지 않는다. 권한 변경은 별도 관리자
    경로에서만 허용된다(user_crud.update_user_db 화이트리스트로도 이중 차단).
    """

    password: str = Field(None, min_length=4, description="비밀번호")


class UserUpdateToken(BaseResponseModel):
    """유저 토큰 수정 요청 스키마."""

    token: str = Field(..., description="휴대폰 토큰")


class UserResponse(UserBase, UserRoleMixin):
    """유저 응답 스키마."""

    id: int = Field(..., description="유저 고유 ID")
    role_name: str | None = Field(None, description="유저 권한 이름(소스코드)")
    created_at: datetime = Field(..., description="생성일시")
    updated_at: datetime = Field(..., description="수정일시")

    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }

    @model_validator(mode="after")
    def set_role_name(self) -> "UserResponse":
        if self.role and not self.role_name:
            self.role_name = self.role.label
        return self


# 이메일 중복 체크 응답 스키마
class UserEmailCheckResponse(BaseResponseModel):
    exists: bool = Field(..., description="이메일 중복 여부")
    message: str | None = Field(None, description="메시지")

    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }
