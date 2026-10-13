import asyncio
from typing import Any, Literal

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.types import TypeDecorator

import app.db.models  # noqa: F401  모든 테이블을 Base.metadata에 올린다.
from app.config import get_settings
from app.db.base import Base
from app.db.engine import Database
from app.db.migrate import run_in_transaction
from app.logging import configure_logging

config = context.config


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    # 앱의 TypeDecorator(UUIDStr 등)를 마이그레이션 파일에 그대로 쓰면 나중에 그 클래스가 바뀔 때
    # 옛 리비전이 깨진다. 실제 저장 타입으로 풀어 쓴다.
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl_instance!r}"
    return False


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=Base.metadata,
        # SQLite는 ALTER TABLE이 제한적이라 열·제약 변경을 테이블 재생성으로 처리한다.
        render_as_batch=True,
        render_item=_render_item,
        **kwargs,
    )


def run_with_connection(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_offline() -> None:
    url = f"sqlite:///{get_settings().data_dir / 'app.db'}"
    _configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


async def run_standalone() -> None:
    # alembic 명령을 직접 부를 때(revision --autogenerate 등). 서버와 platform migrate는
    # app.db.migrate.upgrade_head가 트랜잭션을 연 연결을 attributes로 넘긴다.
    settings = get_settings()
    configure_logging(settings.log_level, secrets=settings.secret_values())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / "app.db")
    try:
        async with db.write_connection() as conn:
            await conn.run_sync(
                lambda sync_conn: run_in_transaction(
                    sync_conn, lambda: run_with_connection(sync_conn)
                )
            )
    finally:
        await db.close()


if context.is_offline_mode():
    run_offline()
elif (shared := config.attributes.get("connection")) is not None:
    run_with_connection(shared)
else:
    asyncio.run(run_standalone())
