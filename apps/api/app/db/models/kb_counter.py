from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KbCounter(Base):
    # 전역 증가 번호(3단계 설계 4.1). unit_no 행이 index.db의 rowid를 발급한다. 번호는
    # 워크스페이스와 무관하게 하나의 수열이어야 rowid와 1:1이라 workspace_id가 없는 전역 테이블이다.
    __tablename__ = "kb_counter"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int]
