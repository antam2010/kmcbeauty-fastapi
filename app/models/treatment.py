from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import backref, relationship

from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.base import Base
from app.models.mixin.timestamp import TimestampMixin


class Treatment(Base, TimestampMixin):
    __tablename__ = "treatment"
    __table_args__ = (
        # 캘린더/리스트 조회: 샵별 + 날짜
        Index("idx_treatment_shop_reserved", "shop_id", "reserved_at"),
        # 상태 보드/필터: 샵별 + 상태 + 날짜
        Index("idx_treatment_shop_status", "shop_id", "status", "reserved_at"),
        # 전화 기반 검색/백필: 샵별 + 고객 전화
        Index("idx_treatment_shop_phone", "shop_id", "customer_phone"),
        {"comment": "시술 예약 테이블"},
    )

    id = Column(Integer, primary_key=True, index=True, comment="시술 예약 ID")

    shop_id = Column(
        Integer,
        ForeignKey("shop.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="샵 ID",
    )

    phonebook_id = Column(
        Integer,
        ForeignKey("phonebook.id"),
        nullable=True,
        index=True,
        comment="시술 대상 고객 ID",
    )

    reserved_at = Column(DateTime, nullable=False, comment="예약 일시")

    memo = Column(Text, nullable=True, comment="메모")

    status = Column(
        SqlEnum(TreatmentStatus, name="treatment_status"),
        nullable=False,
        default=TreatmentStatus.RESERVED,
        comment="예약 상태",
    )

    finished_at = Column(DateTime, nullable=True, comment="시술 완료일시")

    payment_method = Column(
        SqlEnum(PaymentMethod, name="payment_method"),
        nullable=False,
        default=PaymentMethod.CARD,
        comment="결제 수단",
    )

    staff_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="시술 담당자 유저 ID",
    )

    created_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="예약 생성자 유저 ID",
    )

    customer_name = Column(
        String(100),
        nullable=True,
        comment="고객명",
    )

    customer_phone = Column(
        String(20),
        nullable=True,
        comment="고객 전화번호",
    )

    # Relationships
    # 예약 삭제 시 항목은 DB 가 지운다(treatment_id FK ON DELETE CASCADE).
    # passive_deletes 는 부모 삭제 시 로딩만 생략한다. delete-orphan 은 그대로다.
    treatment_items = relationship(
        "TreatmentItem",
        back_populates="treatment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    phonebook = relationship(
        "Phonebook",
        back_populates="treatments",
        foreign_keys=[phonebook_id],
    )

    shop = relationship(
        "Shop",
        back_populates="treatments",
        foreign_keys=[shop_id],
    )

    # User.treatments_as_staff / User.treatments_created(1:N) 는 backref 로 만든다.
    # 유저 삭제 시 두 FK 는 DB 가 SET NULL 한다. ORM 은 시술을 로딩·UPDATE 하지 않는다.
    staff_user = relationship(
        "User",
        foreign_keys=[staff_user_id],
        backref=backref("treatments_as_staff", passive_deletes=True),
    )

    created_user = relationship(
        "User",
        foreign_keys=[created_user_id],
        backref=backref("treatments_created", passive_deletes=True),
    )
