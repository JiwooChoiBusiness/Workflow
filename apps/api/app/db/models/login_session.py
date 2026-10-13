import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import utcnow


class LoginSession(Base):
    __tablename__ = "login_session"

    # 쿠키 값의 sha256(hex). 원문은 DB에 없다.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("account.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String)
