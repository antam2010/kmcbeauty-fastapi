from typing import ClassVar

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.mixin.soft_delete import SoftDeleteMixin
from app.models.mixin.timestamp import TimestampMixin


class TreatmentMenuDetail(Base, SoftDeleteMixin, TimestampMixin):
    __tablename__ = "treatment_menu_detail"
    __table_args__: ClassVar[dict] = {"comment": "시술 메뉴 상세 테이블"}

    id = Column(Integer, primary_key=True, index=True, comment="시술 상세 ID")
    menu_id = Column(
        Integer,
        ForeignKey("treatment_menu.id", ondelete="CASCADE"),
        nullable=False,
        comment="시술 메뉴 대분류 ID",
    )

    name = Column(String(255), nullable=False, comment="시술 항목명")
    duration_min = Column(Integer, nullable=False, comment="기본 시술 시간 (분)")
    base_price = Column(Integer, nullable=False, comment="기본 시술 가격 (원)")

    # 이 항목이 속한 시술 예약 객체와의 관계 (N:1)
    # TreatmentItem.menu_detail 와 양방향 연결됨
    items = relationship(
        "TreatmentItem",
        back_populates="menu_detail",
        cascade="all, delete-orphan",
    )

    menu = relationship(
        "TreatmentMenu",
        back_populates="details",
    )
