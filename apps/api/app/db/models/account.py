import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column


class Account(TimestampMixin, Base):
    # 워크스페이스에 속하지 않는 전역 테이블이다. 1단계는 1행만 둔다.
    __tablename__ = "account"

    id: Mapped[uuid.UUID] = id_column()
    # 저장할 때 소문자로 맞춘다. 비교도 소문자로 한다.
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    password_hash: Mapped[str] = mapped_column(String)
    last_login_at: Mapped[datetime | None]
