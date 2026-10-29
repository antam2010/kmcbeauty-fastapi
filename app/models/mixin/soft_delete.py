from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy.orm import Mapped, mapped_column


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="삭제일시",
    )

    __mapper_args__: ClassVar[dict] = {"eager_defaults": True}

    def soft_delete(self) -> None:
        self.deleted_at = datetime.now(UTC)

    def is_deleted(self) -> bool:
        return self.deleted_at is not None
