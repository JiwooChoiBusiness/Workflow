from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Connection

from app.db.engine import Database

# alembic은 메모리를 아끼려고 마이그레이션하는 함수 안에서만 import한다(설계 2.7).
if TYPE_CHECKING:
    from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parent.parent.parent / "alembic.ini"


class MigrationError(RuntimeError):
    pass


def alembic_config() -> "Config":
    from alembic.config import Config

    return Config(str(ALEMBIC_INI))


def _pragma(connection: Connection, statement: str) -> None:
    # SQLAlchemy를 거치면 자동으로 BEGIN이 먼저 나가므로 DBAPI 커서로 직접 보낸다.
    cursor = connection.connection.cursor()
    try:
        cursor.execute(statement)
    finally:
        cursor.close()


def run_in_transaction(connection: Connection, migrate: Callable[[], None]) -> None:
    # batch 마이그레이션은 새 테이블로 옮긴 뒤 옛 테이블을 DROP한다. 외래 키 검사가 켜져 있으면
    # DROP이 참조 행 때문에 실패하므로 끄고 돌린 뒤, 커밋 전에 위반이 없는지 직접 확인한다.
    # foreign_keys PRAGMA는 트랜잭션 안에서는 무시되어 BEGIN 전에 바꿔야 한다.
    _pragma(connection, "PRAGMA foreign_keys=OFF")
    try:
        with connection.begin():
            migrate()
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"외래 키 위반 {len(violations)}건: {violations[:5]}")
    finally:
        _pragma(connection, "PRAGMA foreign_keys=ON")


async def upgrade_head(db: Database) -> None:
    from alembic import command

    config = alembic_config()

    def upgrade(connection: Connection) -> None:
        config.attributes["connection"] = connection
        run_in_transaction(connection, lambda: command.upgrade(config, "head"))

    # BEGIN IMMEDIATE로 시작해 다른 프로세스(백업 CLI, 떠 있는 서버)와 쓰기 잠금을 다투지 않는다.
    async with db.write_connection() as conn:
        await conn.run_sync(upgrade)


async def current_revision(db: Database) -> str | None:
    from alembic.runtime.migration import MigrationContext

    def read(connection: Connection) -> str | None:
        return MigrationContext.configure(connection).get_current_revision()

    async with db.engine.connect() as conn:
        return await conn.run_sync(read)
