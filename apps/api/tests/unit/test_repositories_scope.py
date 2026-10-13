import importlib
import pkgutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Table, UniqueConstraint, or_, select, true

import app.db.models  # noqa: F401
import app.db.repositories as repositories_package
from app.db.base import Base
from app.db.engine import Database
from app.db.models import Account, Agent, Workspace
from app.db.repositories.base import (
    GLOBAL_TABLES,
    GlobalRepository,
    Repository,
    UnscopedQueryError,
    WorkspaceScopedRepository,
)


def all_repository_classes() -> list[type[Repository[Any]]]:
    for module in pkgutil.iter_modules(repositories_package.__path__):
        importlib.import_module(f"{repositories_package.__name__}.{module.name}")
    found: list[type[Repository[Any]]] = []
    pending: list[type[Repository[Any]]] = [Repository]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            pending.append(sub)
            if sub.__module__.startswith("app.db.repositories.") and "model" in sub.__dict__:
                found.append(sub)
    return found


def tables() -> list[Table]:
    return [cast(Table, table) for table in Base.metadata.sorted_tables]


def test_every_repository_is_scoped_or_an_allowed_global_one() -> None:
    classes = all_repository_classes()

    assert {cls.__name__ for cls in classes} >= {
        "AccountRepository",
        "WorkspaceRepository",
        "LoginSessionRepository",
    }
    for cls in classes:
        table = cast(Table, cls.model.__table__)
        if issubclass(cls, GlobalRepository):
            assert table.name in GLOBAL_TABLES, cls
        else:
            assert issubclass(cls, WorkspaceScopedRepository), cls


def test_every_core_table_has_indexed_workspace_id() -> None:
    for table in tables():
        if table.name in GLOBAL_TABLES:
            assert "workspace_id" not in table.c, table.name
            continue
        column = table.c.get("workspace_id")
        assert column is not None, table.name
        assert not column.nullable, table.name
        assert {fk.target_fullname for fk in column.foreign_keys} == {"workspace.id"}, table.name
        leading = [index.expressions[0] for index in table.indexes] + [
            next(iter(constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        assert any(getattr(item, "name", None) == "workspace_id" for item in leading), table.name


def test_scoped_repository_needs_workspace_id_column() -> None:
    with pytest.raises(TypeError):

        class AccountScoped(WorkspaceScopedRepository[Account]):
            model = Account


def test_global_repository_only_for_global_tables() -> None:
    with pytest.raises(TypeError):

        class AgentGlobal(GlobalRepository[Agent]):
            model = Agent


class AgentRepository(WorkspaceScopedRepository[Agent]):
    model = Agent

    def add(self, slug: str, workspace_id: uuid.UUID | None = None) -> Agent:
        agent = Agent(slug=slug, name=slug)
        if workspace_id is not None:
            agent.workspace_id = workspace_id
        return self._add(agent)

    async def flush(self) -> None:
        await self._flush()

    async def slugs(self) -> list[str]:
        return sorted(await self._scalars(self._select(Agent.slug)))

    async def rename_all(self, name: str) -> None:
        await self._execute(self._update().values(name=name))

    async def delete_all(self) -> None:
        await self._execute(self._delete())

    async def run(self, statement: Any) -> Any:
        return await self._execute(statement)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "scope.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


@pytest.fixture
async def two_workspaces(db: Database) -> tuple[uuid.UUID, uuid.UUID]:
    async with db.write() as session:
        first = Workspace(name="a", slug="a", settings={})
        second = Workspace(name="b", slug="b", settings={})
        session.add_all([first, second])
        await session.flush()
        ids = (first.id, second.id)
    async with db.write() as session:
        AgentRepository(session, ids[0]).add("mine")
        AgentRepository(session, ids[1]).add("theirs")
    return ids


async def test_reads_and_writes_stay_inside_the_workspace(
    db: Database, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    mine, theirs = two_workspaces
    async with db.read() as session:
        other_agent_id = (
            await session.scalars(select(Agent.id).where(Agent.workspace_id == theirs))
        ).one()

    async with db.write() as session:
        repo = AgentRepository(session, mine)
        assert await repo.slugs() == ["mine"]
        assert await repo.get(other_agent_id) is None
        await repo.rename_all("renamed")
        await repo.delete_all()

    async with db.read() as session:
        remaining = (await session.execute(select(Agent.slug, Agent.name))).all()
    assert [tuple(row) for row in remaining] == [("theirs", "theirs")]


@pytest.mark.parametrize(
    "build",
    [
        lambda mine, theirs: select(Agent),
        lambda mine, theirs: select(Agent).where(Agent.slug == "mine"),
        lambda mine, theirs: select(Agent).where(Agent.workspace_id == theirs),
        lambda mine, theirs: select(Agent).where(or_(Agent.workspace_id == mine, true())),
        lambda mine, theirs: select(Agent).where(Agent.workspace_id != mine),
    ],
    ids=["no-filter", "other-filter", "other-workspace", "inside-or", "not-equal"],
)
async def test_unscoped_statements_are_refused(
    db: Database, two_workspaces: tuple[uuid.UUID, uuid.UUID], build: Any
) -> None:
    mine, theirs = two_workspaces
    async with db.read() as session:
        repo = AgentRepository(session, mine)
        with pytest.raises(UnscopedQueryError):
            await repo.run(build(mine, theirs))


async def test_scope_can_be_combined_with_more_conditions(
    db: Database, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    mine, _ = two_workspaces
    async with db.read() as session:
        repo = AgentRepository(session, mine)
        statement = select(Agent.slug).where(Agent.slug == "mine", Agent.workspace_id == mine)

        assert (await repo.run(statement)).scalars().all() == ["mine"]


async def test_add_fills_and_guards_workspace_id(
    db: Database, two_workspaces: tuple[uuid.UUID, uuid.UUID]
) -> None:
    mine, theirs = two_workspaces
    async with db.write() as session:
        repo = AgentRepository(session, mine)
        added = repo.add("second")
        await repo.flush()
        assert added.workspace_id == mine
        with pytest.raises(UnscopedQueryError):
            repo.add("intruder", workspace_id=theirs)


def test_session_is_not_reachable_by_name() -> None:
    repo = AgentRepository(cast(Any, object()), uuid.uuid4())

    assert not hasattr(repo, "session")
    assert not hasattr(repo, "_session")
