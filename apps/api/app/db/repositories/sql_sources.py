import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func

from app.db.models import SqlQueryLog, SqlSource
from app.db.repositories.base import WorkspaceScopedRepository


class SqlSourceRepository(WorkspaceScopedRepository[SqlSource]):
    # 지운(deleted_at) 데이터 소스의 행은 질의 기록의 외래 키 때문에 남긴다. 조회는 살아 있는 행만
    # 본다.
    model = SqlSource

    async def get_active(self, source_id: uuid.UUID) -> SqlSource | None:
        return await self._scalar(
            self._select().where(SqlSource.id == source_id, SqlSource.deleted_at.is_(None))
        )

    async def get_active_by_slug(self, slug: str) -> SqlSource | None:
        return await self._scalar(
            self._select().where(SqlSource.slug == slug, SqlSource.deleted_at.is_(None))
        )

    async def get_many_active(self, source_ids: Collection[uuid.UUID]) -> list[SqlSource]:
        if not source_ids:
            return []
        return await self._scalars(
            self._select().where(SqlSource.id.in_(list(source_ids)), SqlSource.deleted_at.is_(None))
        )

    async def list_active(self) -> list[SqlSource]:
        return await self._scalars(
            self._select()
            .where(SqlSource.deleted_at.is_(None))
            .order_by(SqlSource.created_at.desc(), SqlSource.id.desc())
        )

    async def list_all(self) -> list[SqlSource]:
        # 지운 것까지. 매시 정리가 남은 파일을 가릴 때 쓴다.
        return await self._scalars(self._select().order_by(SqlSource.created_at))

    async def slug_taken(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        statement = self._select(SqlSource.id).where(
            SqlSource.slug == slug, SqlSource.deleted_at.is_(None)
        )
        if exclude_id is not None:
            statement = statement.where(SqlSource.id != exclude_id)
        return await self._scalar(statement.limit(1)) is not None

    def add(self, source: SqlSource) -> SqlSource:
        return self._add(source)

    async def flush(self) -> None:
        await self._flush()


class SqlQueryLogRepository(WorkspaceScopedRepository[SqlQueryLog]):
    model = SqlQueryLog

    async def list_page(
        self, source_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[SqlQueryLog]:
        return await self._scalars(
            self._select()
            .where(SqlQueryLog.source_id == source_id)
            .order_by(SqlQueryLog.created_at.desc(), SqlQueryLog.id.desc())
            .limit(limit)
            .offset(offset)
        )

    async def count(self, source_id: uuid.UUID) -> int:
        statement = self._select(func.count(SqlQueryLog.id)).where(
            SqlQueryLog.source_id == source_id
        )
        return int(await self._scalar(statement) or 0)

    async def delete_before(self, cutoff: datetime) -> int:
        result = await self._execute(self._delete().where(SqlQueryLog.created_at < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)

    def add(self, row: SqlQueryLog) -> SqlQueryLog:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
