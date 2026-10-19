from sqlalchemy import Column, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.mixin.soft_delete import SoftDeleteMixin
from app.models.mixin.timestamp import TimestampMixin


class Shop(Base, SoftDeleteMixin, TimestampMixin):
    __tablename__ = "shop"
    # 유저별 샵 조회(get_user_shops/get_user_shop_by_id)의 잦은 FK 필터를 위한 인덱스.
    # (SPEC-FIX-001 REQ-FIX-005)
    __table_args__ = (Index("ix_shop_user_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False, comment="샵 이름")
    address = Column(String(255), nullable=False, comment="샵 주소")
    address_detail = Column(String(255), comment="샵 상세 주소")
    phone = Column(String(20), comment="샵 전화번호")
    business_number = Column(String(20), comment="사업자 등록 번호")

    # 관계 정의

    # @MX:NOTE: [AUTO] 샵 하드 삭제는 DB 연쇄 삭제에 맡긴다. 자식 FK 는 모두
    #            ON DELETE CASCADE 다. passive_deletes=True 로 자식을 로딩하지 않고
    #            부모 DELETE 만 내보낸다(행 단위 N+1 삭제 방지). 자식 관계는
    #            TreatmentMenu.shop 의 backref(treatment_menus)에도 있다.

    # Shop → Phonebook (1:N)
    phonebook_list = relationship(
        "Phonebook",
        back_populates="shop",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # 이 샵의 소유자와의 관계 (N:1)
    owner = relationship("User", back_populates="shops")

    # Shop → Treatment (1:N)
    treatments = relationship(
        "Treatment",
        back_populates="shop",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Shop → ShopInvite (1:N)
    invites = relationship(
        "ShopInvite",
        back_populates="shop",
        cascade="all, delete",
        passive_deletes=True,
    )
