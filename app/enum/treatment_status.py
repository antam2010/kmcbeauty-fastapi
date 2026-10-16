from enum import Enum


class TreatmentStatus(str, Enum):
    RESERVED = "RESERVED"
    VISITED = "VISITED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    COMPLETED = "COMPLETED"

    @property
    def label(self) -> str:
        return {
            TreatmentStatus.RESERVED: "예약됨",
            TreatmentStatus.VISITED: "방문 및 시술 완료",
            TreatmentStatus.CANCELLED: "취소됨",
            TreatmentStatus.NO_SHOW: "노쇼",
            TreatmentStatus.COMPLETED: "시술 완료",
        }.get(self.value, "Unknown")

    @classmethod
    def unfinished_statuses(cls) -> list[str]:
        return [cls.RESERVED, cls.VISITED]

    # @MX:NOTE: [AUTO] 자동완료 정책 — 직원이 방문 처리한 VISITED 만 자동완료한다.
    #            RESERVED 는 노쇼일 수 있어 종료 시각이 지나도 그대로 두고, 직원이
    #            방문/노쇼를 직접 고른다. 결제 수단 기본값이 CARD 라 자동완료하면
    #            실매출로 잡히고 노쇼로 집계되지 않는다. unfinished_statuses() 로
    #            바꾸지 말 것(조회 조건과 UPDATE 재확인이 함께 쓴다).
    @classmethod
    def autocomplete_statuses(cls) -> list[str]:
        """자동완료 대상 상태(직원이 방문 처리한 VISITED 만)."""
        return [cls.VISITED]

    @classmethod
    def for_expected_sales(cls) -> list[str]:
        """예상매출에 포함할 상태들."""
        return [cls.RESERVED.value, cls.VISITED.value, cls.COMPLETED.value]

    @classmethod
    def for_actual_sales(cls) -> str:
        """실매출(완료 기준)은 COMPLETED만."""
        return cls.COMPLETED.value


class PaymentMethod(str, Enum):
    CARD = "CARD"  # 카드 결제
    CASH = "CASH"  # 현금 결제
    UNPAID = "UNPAID"  # 미수금

    @property
    def label(self) -> str:
        return {
            PaymentMethod.CARD: "카드 결제",
            PaymentMethod.CASH: "현금 결제",
            PaymentMethod.UNPAID: "미수금",
        }.get(self.value, "Unknown")

    @classmethod
    def paid_methods(cls) -> list[str]:
        """결제 완료 방식(카드/현금) 배열 리턴."""
        return [cls.CARD.value, cls.CASH.value]

    @classmethod
    def unpaid_methods(cls) -> list[str]:
        """외상 결제 방식(미수금) 배열 리턴."""
        return [cls.UNPAID.value]
