from typing import ClassVar

from sqlalchemy import Column, ForeignKey, Index, Integer, SmallInteger
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.mixin.timestamp import TimestampMixin


class TreatmentItem(Base, TimestampMixin):
    __tablename__ = "treatment_item"
    # treatment_id FK 는 자동완료/목록 조인에서 자주 필터되므로 인덱스를 추가한다.
    # dict-only __table_args__ 를 (Index(...), {...}) 튜플 형태로 변환.
    # (SPEC-FIX-001 REQ-FIX-005)
    __table_args__: ClassVar[tuple] = (
        Index("ix_treatment_item_treatment_id", "treatment_id"),
        {"comment": "시술 항목 테이블"},
    )

    id = Column(Integer, primary_key=True, index=True, comment="시술 항목 ID")
    treatment_id = Column(
        Integer,
        ForeignKey("treatment.id", ondelete="CASCADE"),
        nullable=False,
        comment="시술 예약 ID",
    )
    menu_detail_id = Column(
        Integer,
        ForeignKey("treatment_menu_detail.id", ondelete="SET NULL"),
        nullable=True,
        comment="시술 상세 ID",
    )

    base_price = Column(
        Integer,
        nullable=False,
        server_default="0",
        comment="기본 가격",
    )

    duration_min = Column(
        Integer,
        nullable=False,
        server_default="0",
        comment="소요 시간 (분)",
    )

    session_no = Column(
        SmallInteger,
        nullable=False,
        server_default="1",
        comment="시술 회차",
    )

    # 이 항목이 속한 시술 예약 객체와의 관계 (N:1)
    # Treatment.items 와 양방향 연결됨
    treatment = relationship("Treatment", back_populates="treatment_items")

    menu_detail = relationship("TreatmentMenuDetail", back_populates="items")
