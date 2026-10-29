from typing import ClassVar

from sqlalchemy import Column, Integer, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import relationship

from app.enum.role import UserRole
from app.models.base import Base
from app.models.mixin.soft_delete import SoftDeleteMixin
from app.models.mixin.timestamp import TimestampMixin


class User(Base, SoftDeleteMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__: ClassVar[dict] = {"comment": "유저 테이블"}

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(50), nullable=False, unique=True, comment="유저 이메일")
    name = Column(String(50), nullable=False, comment="유저 이름")
    password = Column(String(255), nullable=False, comment="비밀번호")
    token = Column(String(255), nullable=True, comment="유저 토큰")

    role = Column(
        SqlEnum(UserRole),
        nullable=False,
        server_default=UserRole.MASTER,
        comment="유저 권한",
    )

    # User → shop (1:N)
    # User는 여러 개의 shop을 가질 수 있다.
    shops = relationship("Shop", back_populates="owner", cascade="all, delete-orphan")

    shop_users = relationship(
        "ShopUser",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
