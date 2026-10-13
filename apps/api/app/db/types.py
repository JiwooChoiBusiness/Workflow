import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import uuid6
from sqlalchemy import JSON, CheckConstraint, Dialect, String, Text
from sqlalchemy.types import TypeDecorator

# 항상 UTC·고정 폭이라 문자열 정렬이 시간 정렬과 같다. 인덱스와 범위 조건이 문자열 비교로 동작한다.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# None을 JSON 'null' 문자열이 아니라 SQL NULL로 저장한다.
JSONValue = JSON(none_as_null=True)


def new_id() -> uuid.UUID:
    return uuid6.uuid7()


def utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDStr(TypeDecorator[uuid.UUID]):
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        return uuid.UUID(value)


class UTCDateTime(TypeDecorator[datetime]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"datetime이 필요하다: {type(value).__name__}")
        # 시간대 없는 값은 로컬 시각인지 UTC인지 알 수 없어 받지 않는다.
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("시간대가 없는 datetime은 저장하지 않는다")
        return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def enum_check(column: str, values: Iterable[str]) -> CheckConstraint:
    # 제약 이름은 naming convention이 ck_<table>_<column>으로 만든다.
    # batch 마이그레이션이 이 이름으로 제약을 찾는다.
    # 열 이름을 따옴표로 감싸는 것은 run.trigger처럼 SQL 예약어인 열이 있어서다.
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f'"{column}" IN ({allowed})', name=column)
