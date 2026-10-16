from sqlalchemy import Column, ForeignKey, Integer, String, and_
from sqlalchemy.orm import backref, relationship

from app.models.base import Base
from app.models.mixin.soft_delete import SoftDeleteMixin
from app.models.mixin.timestamp import TimestampMixin
from app.models.treatment_menu_detail import TreatmentMenuDetail


class TreatmentMenu(Base, SoftDeleteMixin, TimestampMixin):
    __tablename__ = "treatment_menu"
    __table_args__ = ({"comment": "시술 메뉴 대분류 테이블"},)

    id = Column(Integer, primary_key=True, index=True, comment="시술 메뉴 대분류 ID")

    shop_id = Column(
        Integer,
        ForeignKey("shop.id", ondelete="CASCADE"),
        nullable=False,
        comment="샵 ID",
    )

    name = Column(String(255), nullable=False, comment="시술 대분류명")

    # 관계 정의
    # Shop.treatment_menus(1:N) 는 backref 로 만든다. delete cascade 가 없으면 샵
    # 삭제 시 ORM 이 shop_id(NOT NULL)를 NULL 로 UPDATE 해 IntegrityError 가 난다
    # (소프트삭제 메뉴 포함). FK 가 ON DELETE CASCADE 라 passive_deletes 로 맡긴다.
    shop = relationship(
        "Shop",
        backref=backref(
            "treatment_menus",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
    )

    # @MX:NOTE: [AUTO] soft-delete 인지 관계 — 소프트삭제되지 않은 상세만 로딩한다.
    #            (SPEC-FIX-001 REQ-FIX-003)
    #            [D1 진단 결과] 이전의 인접 문자열 리터럴 연결
    #            ("and_(" "...menu_id, " "...deleted_at==None" ")") 은 파이썬이 단일
    #            문자열로 이어붙여 SQLAlchemy 가 정상 파싱했다(깨지지 않았음).
    #            동작 변화 없이 가독성을 위해 callable(lambda) + and_()/.is_(None)
    #            형태로 교체했다. lambda 지연 평가로 mapper 구성 시점에 매핑 클래스를
    #            참조한다 (treatment_menu -> treatment_menu_detail 은 단방향 import 로
    #            순환 없음).
    details = relationship(
        "TreatmentMenuDetail",
        back_populates="menu",  # 반대편(TreatmentMenuDetail.menu)에서도 이 관계 접근
        cascade="all, delete-orphan",  # 부모 없는 상태면 삭제
        passive_deletes=True,  # menu_id FK 가 ON DELETE CASCADE(메뉴 삭제 시 DB 처리)
        primaryjoin=lambda: and_(
            TreatmentMenu.id == TreatmentMenuDetail.menu_id,
            TreatmentMenuDetail.deleted_at.is_(None),
        ),
    )
