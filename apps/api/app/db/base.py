import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import UTCDateTime, UUIDStr, new_id, utcnow

# SQLite batch 마이그레이션은 테이블을 새로 만들어 옮기므로 제약을 이름으로 찾는다.
# 이름이 없으면 나중에 제약을 바꾸거나 지울 수 없다.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {uuid.UUID: UUIDStr(), datetime: UTCDateTime()}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, sort_order=100)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, sort_order=101)


def id_column() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=new_id, sort_order=-100)


def workspace_id_column(*, index: bool = True) -> Mapped[uuid.UUID]:
    # workspace_id가 맨 앞에 오는 복합 인덱스가 있는 테이블은 단일 인덱스를 따로 두지 않는다.
    return mapped_column(ForeignKey("workspace.id"), index=index, sort_order=-90)
