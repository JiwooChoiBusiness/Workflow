import uuid
from typing import Any, ClassVar, cast

from sqlalchemy import BinaryExpression, BindParameter, ColumnElement, Delete, Select, Table, Update
from sqlalchemy import delete as sa_delete
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BooleanClauseList

from app.db.base import Base

# workspace_id가 없는 전역 테이블. 이 셋만 GlobalRepository로 다룰 수 있다.
GLOBAL_TABLES = frozenset({"workspace", "account", "login_session"})


class UnscopedQueryError(RuntimeError):
    pass


def table_of(model: type[Base]) -> Table:
    return cast(Table, model.__table__)


class Repository[ModelT: Base]:
    model: ClassVar[type[Base]]


class GlobalRepository[ModelT: Base](Repository[ModelT]):
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.__dict__.get("model")
        if model is None:
            return
        table = table_of(model)
        if table.name not in GLOBAL_TABLES or "workspace_id" in table.c:
            raise TypeError(
                f"{cls.__name__}: {table.name}은 전역 테이블이 아니다. "
                "WorkspaceScopedRepository를 상속해야 한다"
            )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


class WorkspaceScopedRepository[ModelT: Base](Repository[ModelT]):
    # 세션은 이름 맹글링으로 숨겨 하위 클래스가 직접 쓰지 못하게 한다. 실행은 workspace_id 조건을
    # 검사하는 _execute·_scalars·_scalar로만 하고, 문장은 조건이 이미 붙은 _select·_update·_delete로
    # 시작한다. 조건이 없거나 OR 안에만 있거나 다른 워크스페이스 값이면 UnscopedQueryError.

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.__dict__.get("model")
        if model is None:
            return
        column = table_of(model).c.get("workspace_id")
        if column is None or column.nullable:
            raise TypeError(f"{cls.__name__}: {model.__tablename__}에 workspace_id NOT NULL이 없다")

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.__session = session
        self.workspace_id = workspace_id

    @property
    def _scope_column(self) -> ColumnElement[Any]:
        return table_of(self.model).c.workspace_id

    def _select(self, *entities: Any) -> Select:
        return sa_select(*(entities or (self.model,))).where(
            self._scope_column == self.workspace_id
        )

    def _update(self) -> Update:
        return sa_update(self.model).where(self._scope_column == self.workspace_id)

    def _delete(self) -> Delete:
        return sa_delete(self.model).where(self._scope_column == self.workspace_id)

    def _add(self, obj: ModelT) -> ModelT:
        current = getattr(obj, "workspace_id", None)
        if current is not None and current != self.workspace_id:
            raise UnscopedQueryError("다른 워크스페이스의 행은 추가하지 않는다")
        setattr(obj, "workspace_id", self.workspace_id)  # noqa: B010
        self.__session.add(obj)
        return obj

    async def _flush(self) -> None:
        await self.__session.flush()

    async def _execute(self, statement: Select | Update | Delete) -> Result:
        self._check_scope(statement)
        return await self.__session.execute(statement)

    async def _scalars(self, statement: Select) -> list[Any]:
        return list((await self._execute(statement)).scalars().all())

    async def _scalar(self, statement: Select) -> Any:
        return (await self._execute(statement)).scalar_one_or_none()

    async def get(self, id: uuid.UUID) -> ModelT | None:
        return await self._scalar(self._select().where(table_of(self.model).c.id == id))

    def _check_scope(self, statement: Select | Update | Delete) -> None:
        if not _requires(statement.whereclause, self._scope_column, self.workspace_id):
            raise UnscopedQueryError(
                f"{type(self).__name__}: workspace_id 조건이 없는 문장은 실행하지 않는다"
            )


def _requires(clause: ColumnElement[Any] | None, column: ColumnElement[Any], value: Any) -> bool:
    # OR 안에 들어간 조건은 범위를 보장하지 못하므로 최상위 AND 항만 본다.
    if clause is None:
        return False
    if isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
        return any(_requires(item, column, value) for item in clause.clauses)
    if not isinstance(clause, BinaryExpression) or clause.operator is not operators.eq:
        return False
    left, right = clause.left, clause.right
    if isinstance(left, BindParameter):
        left, right = right, left
    return (
        isinstance(right, BindParameter)
        and _same_column(left, column)
        and right.effective_value == value
    )


def _same_column(candidate: Any, column: ColumnElement[Any]) -> bool:
    # ORM 속성에서 나온 열은 주석(annotation)이 붙은 사본이라 is로 비교할 수 없다.
    return getattr(candidate, "table", None) is getattr(column, "table", None) and getattr(
        candidate, "name", None
    ) == getattr(column, "name", None)
