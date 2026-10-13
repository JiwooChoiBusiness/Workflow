"""에이전트, 초안, 발행 버전(설계 3.12, 5.1, 5.3, 6.2).

발행은 초안을 번호 있는 불변 버전으로 복제하고, 미리보기 작업공간이 있으면 그 zip 스냅샷을
버전에 붙인다(없으면 지금 발행 버전의 스냅샷을 이어받는다). 새 웹앱·API·과업 세션의 작업공간은
그 스냅샷에서 시작한다(app/services/run_executor.py).
런(M6)은 load_runtime_version으로 버전 설정·복호화한 환경변수·스킬·첨부 파일을 받고,
provision_content로 작업공간 초기화에 넘길 내용을 만든다.
"""

import asyncio
import copy
import re
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import Agent, AgentVersion, Skill, SkillVersion, Snapshot
from app.db.models.enums import (
    AgentVersionStatus,
    RunStatus,
    SessionSource,
    SkillScope,
    SnapshotKind,
)
from app.db.repositories.agent_files import AgentFileRepository
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.agents import AgentRepository
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.skill_versions import SkillVersionRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.llm.catalog import get_capabilities
from app.sandbox.base import SandboxBackend, SandboxError, SnapshotInfo, WorkspaceHandle
from app.sandbox.content import AgentFileRef, SkillPackageRef, StorageProvisionContent
from app.sandbox.provisioning import SnapshotSource
from app.schemas.agent import AgentCreate, AgentOut, AgentPatch, PublishOut, SnapshotOut
from app.schemas.agent_version import (
    TOOL_GROUPS,
    AgentVersionConfig,
    AgentVersionList,
    AgentVersionOut,
    AgentVersionSummary,
    DraftUpdate,
    EnvVarIn,
    EnvVarOut,
    ModelConfig,
    SandboxConfig,
    StoredEnvVar,
)
from app.schemas.webapp import WebAppOut
from app.schemas.workspace import WorkspaceSettings
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.services.errors import Problems, ServiceError, not_found
from app.services.runs import workspace_handle
from app.storage.base import StorageBackend, StorageError, snapshot_key

log = structlog.get_logger("app.services.agents")

# 설계 4.8: 에이전트당 라이브러리 스킬 20개 + 임베디드 스킬 20개.
MAX_LIBRARY_SKILLS = 20
MAX_EMBEDDED_SKILLS = 20
_SLUG_MAX = 64
_SLUG_ATTEMPTS = 1000
_NON_SLUG = re.compile(r"[^a-z0-9]+")


class ModelDirectory(Protocol):
    """초안의 model_id 검사에 쓰는 라이브 모델 목록. 콘솔 /models가 받아 둔 목록을 그대로 쓴다."""

    def cached_live_models(self) -> list[str] | None: ...


class ResolvedSkill(BaseModel):
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    scope: Literal["library", "embedded"]
    slug: str
    # 그 버전 SKILL.md 프런트매터의 description. 시스템 프롬프트의 스킬 목록에 쓴다(4.8).
    description: str
    storage_key: str

    @property
    def path(self) -> str:
        """작업공간 상대 경로."""
        return f"skills/{self.slug}/SKILL.md"


class ResolvedFile(BaseModel):
    file_id: uuid.UUID
    filename: str
    mime: str
    size_bytes: int
    storage_key: str


class RuntimeVersion(BaseModel):
    """런이 쓰는 에이전트 버전. 스킬은 이름이 겹치면 임베디드를 남긴 결과다."""

    workspace_id: uuid.UUID
    agent_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int | None
    config: AgentVersionConfig
    # 복호화한 환경변수. 도구 프로세스 환경에만 넣고 이벤트·로그에는 남기지 않는다.
    env: dict[str, str] = Field(repr=False)
    skills: list[ResolvedSkill]
    files: list[ResolvedFile]
    # 발행 스냅샷(3.12). 초안은 늘 None이다.
    snapshot_id: uuid.UUID | None = None


@dataclass(frozen=True)
class _NewSnapshot:
    id: uuid.UUID
    key: str
    session_id: uuid.UUID
    info: SnapshotInfo


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_text.lower()).strip("-")[:_SLUG_MAX].strip("-")


def resolve_skills(
    ordered_ids: Sequence[uuid.UUID], rows: Sequence[tuple[SkillVersion, Skill]]
) -> list[ResolvedSkill]:
    """버전의 skill_version_ids를 작업공간에 풀 스킬 목록으로 바꾼다.

    slug가 같으면 임베디드 스킬을 남긴다(4.8).
    """
    by_id = {version.id: (version, skill) for version, skill in rows}
    chosen: dict[str, ResolvedSkill] = {}
    for version_id in ordered_ids:
        row = by_id.get(version_id)
        if row is None:
            log.warning("agent.skill_version_missing", skill_version_id=str(version_id))
            continue
        version, skill = row
        candidate = ResolvedSkill(
            skill_id=skill.id,
            skill_version_id=version.id,
            scope="embedded" if skill.scope == SkillScope.EMBEDDED else "library",
            slug=skill.slug,
            description=str(version.manifest.get("description", skill.description)),
            storage_key=version.storage_key,
        )
        current = chosen.get(skill.slug)
        if current is None or (current.scope == "library" and candidate.scope == "embedded"):
            chosen[skill.slug] = candidate
    return list(chosen.values())


async def snapshot_source(
    db: Database, storage: StorageBackend, workspace_id: uuid.UUID, snapshot_id: uuid.UUID
) -> SnapshotSource:
    """버전의 발행 스냅샷 zip(3.12). 행이나 파일이 없으면 ServiceError(snapshot_missing)."""
    async with db.read() as session:
        row = await SnapshotRepository(session, workspace_id).get(snapshot_id)
    if row is None:
        raise ServiceError(404, "snapshot_missing", f"스냅샷 기록이 없다: {snapshot_id}")
    try:
        path = storage.local_path(row.storage_key)
    except StorageError as exc:
        raise ServiceError(404, "snapshot_missing", exc.message) from None
    if not await asyncio.to_thread(path.is_file):
        raise ServiceError(
            404, "snapshot_missing", f"스냅샷 파일이 저장소에 없다: {row.storage_key}"
        )
    return SnapshotSource(snapshot_id=row.id, path=path, sha256=row.sha256)


def provision_content(storage: StorageBackend, runtime: RuntimeVersion) -> StorageProvisionContent:
    """WorkspaceProvisioner.ensure(content=...)에 넘길 내용."""
    return StorageProvisionContent(
        storage,
        files=[
            AgentFileRef(filename=item.filename, storage_key=item.storage_key)
            for item in runtime.files
        ],
        skills=[
            SkillPackageRef(slug=item.slug, storage_key=item.storage_key) for item in runtime.skills
        ],
    )


def _uuids(values: Sequence[str]) -> list[uuid.UUID]:
    return [uuid.UUID(value) for value in values]


def _config_dict(version: AgentVersion) -> dict[str, Any]:
    return {
        "instructions": version.instructions,
        "model": version.model,
        "tools": version.tools,
        "limits": version.limits,
        "sandbox": version.sandbox,
        "approval_mode": version.approval_mode,
        "skill_version_ids": version.skill_version_ids,
        "file_ids": version.file_ids,
    }


def _agent_out(agent: Agent, published: AgentVersion | None) -> AgentOut:
    return AgentOut(
        id=agent.id,
        slug=agent.slug,
        name=agent.name,
        icon=agent.icon,
        description=agent.description,
        webapp_enabled=agent.webapp_enabled,
        draft_version_id=agent.draft_version_id,
        published_version_id=agent.published_version_id,
        published_version_number=published.version_number if published else None,
        published_at=published.published_at if published else None,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _snapshot_out(row: Snapshot, *, inherited: bool) -> SnapshotOut:
    return SnapshotOut(
        id=row.id,
        file_count=row.file_count,
        size_bytes=row.size_bytes,
        raw_bytes=row.raw_bytes,
        inherited=inherited,
        session_id=row.session_id,
        created_at=row.created_at,
    )


def _draft_problems(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    # 발행 요청 본문에는 이 필드들이 없으므로 위치를 초안 기준으로 바꿔 알려 준다.
    return [{**item, "loc": ["draft", *list(item["loc"])[1:]]} for item in items]


def _slug_taken(slug: str | None) -> ServiceError:
    if slug is None:
        return ServiceError(409, "slug_taken", "slug가 다른 에이전트와 겹친다")
    return ServiceError(409, "slug_taken", f"이미 쓰는 slug다: {slug}", details={"slug": slug})


class AgentService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        models: ModelDirectory | None = None,
        backend: SandboxBackend | None = None,
        storage: StorageBackend | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._models = models
        # 발행 스냅샷을 만들 때만 쓴다. 없으면 스냅샷은 이어받기만 한다.
        self._backend = backend
        self._storage = storage
        self._clock = clock
        self._cipher_cache: SecretCipher | None = None

    def _cipher(self) -> SecretCipher:
        if self._cipher_cache is None:
            try:
                self._cipher_cache = cipher_from_settings(self._settings)
            except CryptoError as exc:
                raise ServiceError(
                    503,
                    "encryption_key_missing",
                    f"환경변수 값을 암호화할 수 없다: {exc}",
                ) from None
        return self._cipher_cache

    async def list_agents(self, workspace_id: uuid.UUID) -> list[AgentOut]:
        async with self._db.read() as session:
            agents = await AgentRepository(session, workspace_id).list_active()
            published = await AgentVersionRepository(session, workspace_id).get_many(
                [agent.published_version_id for agent in agents if agent.published_version_id]
            )
        by_id = {version.id: version for version in published}
        return [
            _agent_out(
                agent,
                by_id.get(agent.published_version_id) if agent.published_version_id else None,
            )
            for agent in agents
        ]

    async def get_agent(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> AgentOut:
        async with self._db.read() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            return _agent_out(agent, await self._published(session, workspace_id, agent))

    async def create_agent(self, workspace_id: uuid.UUID, body: AgentCreate) -> AgentOut:
        try:
            async with self._db.write() as session:
                agents = AgentRepository(session, workspace_id)
                if body.slug is not None:
                    if await agents.slug_taken(body.slug):
                        raise _slug_taken(body.slug)
                    slug = body.slug
                else:
                    slug = await self._free_slug(agents, body.name)
                defaults = await self._workspace_defaults(session, workspace_id)
                agent = agents.add(
                    Agent(
                        id=new_id(),
                        slug=slug,
                        name=body.name,
                        icon=body.icon,
                        description=body.description,
                        webapp_enabled=False,
                    )
                )
                await agents.flush()
                versions = AgentVersionRepository(session, workspace_id)
                draft = versions.add(
                    AgentVersion(
                        id=new_id(),
                        agent_id=agent.id,
                        version_number=None,
                        status=AgentVersionStatus.DRAFT,
                        instructions="",
                        model=ModelConfig().model_dump(mode="json"),
                        tools=list(TOOL_GROUPS),
                        limits=defaults.default_limits.model_dump(mode="json"),
                        sandbox=SandboxConfig().model_dump(mode="json"),
                        approval_mode=defaults.approval_mode_default,
                        env_vars=[],
                        skill_version_ids=[],
                        file_ids=[],
                    )
                )
                await versions.flush()
                agent.draft_version_id = draft.id
                await agents.flush()
                out = _agent_out(agent, None)
        except IntegrityError:
            # 같은 프로세스의 쓰기는 락으로 줄을 서므로 여기까지 오는 것은
            # 외부에서 DB를 고친 경우다.
            raise _slug_taken(body.slug) from None
        log.info("agent.created", agent_id=str(out.id), slug=out.slug)
        return out

    async def update_agent(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, patch: AgentPatch
    ) -> AgentOut:
        try:
            async with self._db.write() as session:
                agents = AgentRepository(session, workspace_id)
                agent = await self._active_agent(session, workspace_id, agent_id)
                if patch.slug is not None and patch.slug != agent.slug:
                    if await agents.slug_taken(patch.slug, exclude_id=agent.id):
                        raise _slug_taken(patch.slug)
                    agent.slug = patch.slug
                if patch.name is not None:
                    agent.name = patch.name
                if "icon" in patch.model_fields_set:
                    agent.icon = patch.icon
                if patch.description is not None:
                    agent.description = patch.description
                if patch.webapp_enabled is not None:
                    agent.webapp_enabled = patch.webapp_enabled
                await agents.flush()
                out = _agent_out(agent, await self._published(session, workspace_id, agent))
        except IntegrityError:
            raise _slug_taken(patch.slug) from None
        return out

    async def delete_agent(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        async with self._db.write() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            now = self._clock()
            # 행은 로그(세션·런)를 위해 남기고 slug는 다시 쓸 수 있게 된다(부분 유니크 인덱스).
            agent.deleted_at = now
            # 지운 에이전트의 키가 서비스 API를 계속 부르지 못하게 바로 폐기한다.
            await ApiKeyRepository(session, workspace_id).revoke_all_for_agent(agent.id, now)
        log.info("agent.deleted", agent_id=str(agent_id))

    async def get_draft(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> AgentVersionOut:
        async with self._db.read() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            draft = await self._draft(session, workspace_id, agent)
        return self._version_out(draft)

    async def save_draft(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, body: DraftUpdate
    ) -> AgentVersionOut:
        problems = Problems()
        self._check_sandbox(body.sandbox, problems)
        self._check_model(body.model, problems, allow_unlisted=body.allow_unlisted_model)
        async with self._db.write() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            draft = await self._draft(session, workspace_id, agent)
            if draft.updated_at != body.updated_at:
                raise ServiceError(
                    409,
                    "draft_conflict",
                    "초안이 다른 곳에서 먼저 저장되었다. 다시 불러온 뒤 고쳐서 저장한다",
                    details={"updated_at": draft.updated_at.isoformat()},
                )
            await self._check_references(
                session, workspace_id, agent.id, body.skill_version_ids, body.file_ids, problems
            )
            env_vars = self._merge_env(draft.env_vars, body.env_vars, problems)
            problems.raise_if_any()
            draft.instructions = body.instructions
            draft.model = body.model.model_dump(mode="json")
            draft.tools = list(body.tools)
            draft.limits = body.limits.model_dump(mode="json")
            draft.sandbox = body.sandbox.model_dump(mode="json")
            draft.approval_mode = body.approval_mode
            draft.env_vars = env_vars
            draft.skill_version_ids = [str(value) for value in body.skill_version_ids]
            draft.file_ids = [str(value) for value in body.file_ids]
            draft.updated_at = self._next_stamp(draft.updated_at)
            await AgentVersionRepository(session, workspace_id).flush()
        return self._version_out(draft)

    async def publish(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, change_note: str | None = None
    ) -> PublishOut:
        """초안을 발행 버전으로 복제하고 스냅샷을 붙인다(3.12).

        zip은 쓰기 잠금 밖에서 만든다(파일이 수천 개면 수 초 걸린다). 그 사이 초안이 바뀌었을 수
        있어 쓰기 트랜잭션에서 초안을 다시 검사하고 그때의 값을 복제한다.
        """
        async with self._db.read() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            draft = await self._draft(session, workspace_id, agent)
            await self._check_publishable(session, workspace_id, agent.id, draft)
            source = await self._preview_workspace(session, workspace_id, agent.id)
        created = await self._create_snapshot(workspace_id, *source) if source else None
        try:
            async with self._db.write() as session:
                agent = await self._active_agent(session, workspace_id, agent_id)
                draft = await self._draft(session, workspace_id, agent)
                await self._check_publishable(session, workspace_id, agent.id, draft)
                previous = await self._published(session, workspace_id, agent)
                versions = AgentVersionRepository(session, workspace_id)
                # 초안은 남아 계속 편집하고, 발행본은 번호를 받은 불변 사본이다(5.3).
                version = versions.add(
                    AgentVersion(
                        id=new_id(),
                        agent_id=agent.id,
                        version_number=await versions.next_version_number(agent.id),
                        status=AgentVersionStatus.PUBLISHED,
                        instructions=draft.instructions,
                        model=copy.deepcopy(draft.model),
                        tools=list(draft.tools),
                        limits=copy.deepcopy(draft.limits),
                        sandbox=copy.deepcopy(draft.sandbox),
                        approval_mode=draft.approval_mode,
                        env_vars=copy.deepcopy(draft.env_vars),
                        skill_version_ids=list(draft.skill_version_ids),
                        file_ids=list(draft.file_ids),
                        snapshot_id=None,
                        published_at=self._clock(),
                        change_note=change_note or None,
                    )
                )
                await versions.flush()
                snapshots = SnapshotRepository(session, workspace_id)
                snapshot: Snapshot | None = None
                if created is not None:
                    snapshot = snapshots.add(
                        Snapshot(
                            id=created.id,
                            kind=SnapshotKind.PUBLISHED,
                            agent_version_id=version.id,
                            session_id=created.session_id,
                            storage_key=created.key,
                            size_bytes=created.info.size_bytes,
                            raw_bytes=created.info.raw_bytes,
                            file_count=created.info.file_count,
                            sha256=created.info.sha256,
                        )
                    )
                    await snapshots.flush()
                elif previous is not None and previous.snapshot_id is not None:
                    # 롤백했다면 그 버전이 지금 발행본이므로 롤백한 상태를 이어받는다.
                    snapshot = await snapshots.get(previous.snapshot_id)
                version.snapshot_id = snapshot.id if snapshot is not None else None
                agent.published_version_id = version.id
                await AgentRepository(session, workspace_id).flush()
                out = PublishOut(
                    agent=_agent_out(agent, version),
                    version=self._version_out(version),
                    snapshot=(
                        _snapshot_out(snapshot, inherited=created is None)
                        if snapshot is not None
                        else None
                    ),
                )
        except BaseException:
            if created is not None and self._storage is not None:
                await self._storage.delete(created.key)
            raise
        log.info(
            "agent.published",
            agent_id=str(agent_id),
            version_id=str(out.version.id),
            version_number=out.version.version_number,
            snapshot_id=str(out.snapshot.id) if out.snapshot is not None else None,
            snapshot_inherited=out.snapshot.inherited if out.snapshot is not None else None,
        )
        return out

    async def _check_publishable(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        draft: AgentVersion,
    ) -> None:
        """저장 뒤에 바뀐 것(지운 스킬, 낮춘 전역 상한 등)을 발행 전에 다시 본다(12.2 M5)."""
        try:
            config = AgentVersionConfig.model_validate(_config_dict(draft))
        except ValidationError as exc:
            raise ServiceError(
                422,
                "draft_invalid",
                "저장된 초안을 읽지 못했다. 초안을 다시 저장한 뒤 발행한다",
                details=[
                    {"loc": ["draft", *error["loc"]], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ],
            ) from None
        problems = Problems()
        self._check_sandbox(config.sandbox, problems)
        # allow_unlisted_model로 저장한 모델일 수 있어 목록 검사는 다시 하지 않는다.
        self._check_model(config.model, problems, allow_unlisted=True)
        await self._check_references(
            session, workspace_id, agent_id, config.skill_version_ids, config.file_ids, problems
        )
        if problems.items:
            raise ServiceError(
                422,
                "draft_invalid",
                "초안에 발행할 수 없는 설정이 있다. 고쳐 저장한 뒤 다시 발행한다",
                details=_draft_problems(problems.items),
            )

    async def _preview_workspace(
        self, session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID
    ) -> tuple[uuid.UUID, WorkspaceHandle] | None:
        """스냅샷을 뜰 미리보기 작업공간. 폴더가 남아 있는 것 중 가장 최근에 쓴 것이다."""
        if self._backend is None or self._storage is None:
            return None
        instances = await WorkspaceInstanceRepository(session, workspace_id).list_for_agent_source(
            agent_id, SessionSource.PREVIEW
        )
        runs = RunRepository(session, workspace_id)
        for instance in instances:
            handle = workspace_handle(self._backend, instance.session_id, instance)
            if not await asyncio.to_thread(handle.root.is_dir):
                continue
            active = await runs.active_for_session(instance.session_id)
            if active is not None and active.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                # 도구가 파일을 쓰는 도중의 상태를 zip에 담지 않는다.
                raise ServiceError(
                    409,
                    "run_in_progress",
                    "미리보기에서 런이 실행 중이다. 끝난 뒤 발행한다",
                    details={"session_id": str(instance.session_id), "run_id": str(active.id)},
                )
            return instance.session_id, handle
        return None

    async def _create_snapshot(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, handle: WorkspaceHandle
    ) -> _NewSnapshot:
        if self._backend is None or self._storage is None:
            raise ServiceError(500, "snapshot_unavailable", "스냅샷을 만들 저장소가 없다")
        snapshot_id = new_id()
        key = snapshot_key(workspace_id, snapshot_id)
        try:
            info = await self._backend.snapshot(handle, self._storage.local_path(key))
        except SandboxError as exc:
            raise ServiceError(exc.status, exc.code, exc.message) from None
        except OSError as exc:
            raise ServiceError(
                500, "snapshot_failed", f"작업공간을 zip으로 묶지 못했다: {exc}"
            ) from None
        log.info(
            "snapshot.created",
            snapshot_id=str(snapshot_id),
            session_id=str(session_id),
            file_count=info.file_count,
            size_bytes=info.size_bytes,
        )
        return _NewSnapshot(id=snapshot_id, key=key, session_id=session_id, info=info)

    async def get_webapp(self, workspace_id: uuid.UUID, slug: str) -> WebAppOut:
        """웹앱(/apps/<slug>)으로 공개한 에이전트. 발행됐고 webapp_enabled여야 한다(6.1)."""
        async with self._db.read() as session:
            agent = await AgentRepository(session, workspace_id).get_active_by_slug(slug)
            version = await self._published(session, workspace_id, agent) if agent else None
        if agent is None or not agent.webapp_enabled or version is None:
            raise ServiceError(404, "app_not_found", "웹앱으로 공개한 에이전트가 아니다")
        return WebAppOut(
            agent_id=agent.id,
            slug=agent.slug,
            name=agent.name,
            icon=agent.icon,
            description=agent.description,
            version_id=version.id,
            version_number=version.version_number or 0,
            published_at=version.published_at,
            approval_mode=version.approval_mode,  # pyright: ignore[reportArgumentType]
        )

    async def list_versions(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> AgentVersionList:
        async with self._db.read() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            published = await AgentVersionRepository(session, workspace_id).list_published(agent.id)
        return AgentVersionList(
            items=[
                AgentVersionSummary(
                    id=version.id,
                    version_number=version.version_number or 0,
                    published_at=version.published_at,
                    change_note=version.change_note,
                    is_current=version.id == agent.published_version_id,
                    created_at=version.created_at,
                )
                for version in published
            ]
        )

    async def get_version(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersionOut:
        async with self._db.read() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            version = await AgentVersionRepository(session, workspace_id).get_for_agent(
                agent.id, version_id
            )
        if version is None:
            raise not_found("에이전트 버전")
        return self._version_out(version)

    async def restore_version(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentOut:
        """롤백: 이전 발행 버전을 다시 발행 버전으로 지정한다(5.3). 초안은 그대로 둔다."""
        async with self._db.write() as session:
            agent = await self._active_agent(session, workspace_id, agent_id)
            version = await AgentVersionRepository(session, workspace_id).get_for_agent(
                agent.id, version_id
            )
            if version is None:
                raise not_found("에이전트 버전")
            if version.version_number is None:
                raise ServiceError(
                    409,
                    "version_not_published",
                    "초안은 발행 버전으로 지정할 수 없다. 먼저 발행한다",
                )
            agent.published_version_id = version.id
            await AgentRepository(session, workspace_id).flush()
            out = _agent_out(agent, version)
        log.info(
            "agent.rolled_back",
            agent_id=str(agent_id),
            version_number=out.published_version_number,
        )
        return out

    async def load_runtime_version(
        self, workspace_id: uuid.UUID, version_id: uuid.UUID
    ) -> RuntimeVersion:
        async with self._db.read() as session:
            version = await AgentVersionRepository(session, workspace_id).get(version_id)
            if version is None:
                raise not_found("에이전트 버전")
            skill_ids = _uuids(version.skill_version_ids)
            file_ids = _uuids(version.file_ids)
            skill_rows = await SkillVersionRepository(session, workspace_id).get_many_with_skill(
                skill_ids
            )
            file_rows = await AgentFileRepository(session, workspace_id).get_many(file_ids)
        files_by_id = {row.id: row for row in file_rows}
        files: list[ResolvedFile] = []
        for file_id in file_ids:
            row = files_by_id.get(file_id)
            if row is None:
                log.warning("agent.file_missing", file_id=str(file_id))
                continue
            files.append(
                ResolvedFile(
                    file_id=row.id,
                    filename=row.filename,
                    mime=row.mime,
                    size_bytes=row.size_bytes,
                    storage_key=row.storage_key,
                )
            )
        return RuntimeVersion(
            workspace_id=workspace_id,
            agent_id=version.agent_id,
            version_id=version.id,
            version_number=version.version_number,
            config=AgentVersionConfig.model_validate(_config_dict(version)),
            env=self._decrypt_env(version.env_vars),
            skills=resolve_skills(skill_ids, skill_rows),
            files=files,
            snapshot_id=version.snapshot_id,
        )

    async def _active_agent(
        self, session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Agent:
        agent = await AgentRepository(session, workspace_id).get_active(agent_id)
        if agent is None:
            raise not_found("에이전트")
        return agent

    async def _draft(
        self, session: AsyncSession, workspace_id: uuid.UUID, agent: Agent
    ) -> AgentVersion:
        draft = await AgentVersionRepository(session, workspace_id).get_draft(agent.id)
        if draft is None:
            raise not_found("초안")
        return draft

    async def _published(
        self, session: AsyncSession, workspace_id: uuid.UUID, agent: Agent
    ) -> AgentVersion | None:
        if agent.published_version_id is None:
            return None
        return await AgentVersionRepository(session, workspace_id).get(agent.published_version_id)

    async def _workspace_defaults(
        self, session: AsyncSession, workspace_id: uuid.UUID
    ) -> WorkspaceSettings:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is not None:
            try:
                return WorkspaceSettings.model_validate(workspace.settings)
            except ValidationError:
                log.warning("workspace.settings_invalid", workspace_id=str(workspace_id))
        return WorkspaceSettings(approval_mode_default=self._settings.approval_mode_default)

    async def _free_slug(self, agents: AgentRepository, name: str) -> str:
        base = slugify(name) or "agent"
        candidate = base
        for number in range(2, _SLUG_ATTEMPTS):
            if not await agents.slug_taken(candidate):
                return candidate
            suffix = f"-{number}"
            candidate = base[: _SLUG_MAX - len(suffix)].rstrip("-") + suffix
        raise _slug_taken(candidate)

    def _next_stamp(self, previous: datetime) -> datetime:
        # 낙관적 잠금 값이 저장할 때마다 반드시 바뀌어야 한다. Windows 시계 해상도(약 15ms) 안에서
        # 두 번 저장해도 같은 값이 나오지 않게 한다.
        now = self._clock()
        return now if now > previous else previous + timedelta(microseconds=1)

    def _check_sandbox(self, sandbox: SandboxConfig, problems: Problems) -> None:
        memory_max = self._settings.sandbox_memory_limit_mb
        if sandbox.memory_limit_mb is not None and sandbox.memory_limit_mb > memory_max:
            problems.add(
                ("sandbox", "memory_limit_mb"),
                "memory_limit_exceeded",
                f"전역 상한 SANDBOX_MEMORY_LIMIT_MB({memory_max}MB) 이하여야 한다",
                max=memory_max,
            )
        processes_max = self._settings.sandbox_max_processes
        if sandbox.max_processes is not None and sandbox.max_processes > processes_max:
            problems.add(
                ("sandbox", "max_processes"),
                "max_processes_exceeded",
                f"전역 상한 SANDBOX_MAX_PROCESSES({processes_max}) 이하여야 한다",
                max=processes_max,
            )

    def _check_model(self, model: ModelConfig, problems: Problems, *, allow_unlisted: bool) -> None:
        fallback = list(self._settings.gemini_model_fallback)
        model_id = model.model_id
        if model_id is not None and not allow_unlisted:
            live = self._models.cached_live_models() if self._models is not None else None
            allowed = [*fallback, *(item for item in live or () if item not in fallback)]
            if model_id not in allowed:
                reason = (
                    "폴백 목록과 라이브 모델 목록에 없는 모델이다"
                    if live is not None
                    else "라이브 모델 목록을 받지 못해 폴백 목록(GEMINI_MODEL_FALLBACK)의 모델만 "
                    "쓸 수 있다"
                )
                problems.add(
                    ("model", "model_id"),
                    "model_not_available",
                    f"{reason}. 그래도 쓰려면 allow_unlisted_model을 true로 보낸다",
                    allowed=allowed,
                    live_list=live is not None,
                )
        effective = model_id or (fallback[0] if fallback else None)
        if effective is None:
            return
        caps = get_capabilities(effective)
        efforts = list(caps.reasoning_efforts) if caps.supports_reasoning_effort else []
        effort = model.params.reasoning_effort
        if effort is not None and effort not in efforts:
            problems.add(
                ("model", "params", "reasoning_effort"),
                "reasoning_effort_unsupported",
                f"{effective} 모델은 reasoning_effort로 {', '.join(efforts) or '아무 값도'} 받는다",
                model=effective,
                allowed=efforts,
            )
        if model.params.max_output_tokens > caps.max_output_tokens:
            problems.add(
                ("model", "params", "max_output_tokens"),
                "max_output_tokens_exceeded",
                f"{effective} 모델의 출력 상한({caps.max_output_tokens}) 이하여야 한다",
                model=effective,
                max=caps.max_output_tokens,
            )

    async def _check_references(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        skill_version_ids: Sequence[uuid.UUID],
        file_ids: Sequence[uuid.UUID],
        problems: Problems,
    ) -> None:
        rows = await SkillVersionRepository(session, workspace_id).get_many_with_skill(
            skill_version_ids
        )
        by_id = {version.id: (version, skill) for version, skill in rows}
        seen: set[uuid.UUID] = set()
        counts = {SkillScope.LIBRARY: 0, SkillScope.EMBEDDED: 0}
        for index, version_id in enumerate(skill_version_ids):
            loc = ("skill_version_ids", index)
            row = by_id.get(version_id)
            # 다른 에이전트의 임베디드 스킬은 있어도 없는 것으로 본다.
            if row is None or (row[1].scope == SkillScope.EMBEDDED and row[1].agent_id != agent_id):
                problems.add(
                    loc,
                    "skill_version_not_found",
                    "이 에이전트가 쓸 수 있는 스킬 버전이 아니다",
                    id=str(version_id),
                )
                continue
            _, skill = row
            if skill.deleted_at is not None:
                problems.add(
                    loc, "skill_deleted", f"삭제된 스킬이다: {skill.slug}", slug=skill.slug
                )
                continue
            if skill.id in seen:
                problems.add(
                    loc,
                    "skill_duplicate",
                    f"같은 스킬({skill.slug})의 버전이 둘 이상 들어 있다",
                    slug=skill.slug,
                )
                continue
            seen.add(skill.id)
            counts[SkillScope(skill.scope)] += 1
        for scope, limit, label in (
            (SkillScope.LIBRARY, MAX_LIBRARY_SKILLS, "라이브러리"),
            (SkillScope.EMBEDDED, MAX_EMBEDDED_SKILLS, "임베디드"),
        ):
            if counts[scope] > limit:
                problems.add(
                    ("skill_version_ids",),
                    f"too_many_{scope.value}_skills",
                    f"{label} 스킬은 에이전트당 {limit}개까지다(지금 {counts[scope]}개)",
                    max=limit,
                )

        files = await AgentFileRepository(session, workspace_id).get_many(file_ids)
        files_by_id = {row.id: row for row in files}
        names: set[str] = set()
        for index, file_id in enumerate(file_ids):
            loc = ("file_ids", index)
            row = files_by_id.get(file_id)
            if row is None or row.agent_id != agent_id:
                problems.add(loc, "file_not_found", "이 에이전트의 파일이 아니다", id=str(file_id))
                continue
            # 작업공간의 agent-files/에서 서로 덮어쓰지 않게 이름이 겹치면(대소문자 무시)
            # 받지 않는다.
            folded = row.filename.casefold()
            if folded in names:
                problems.add(
                    loc,
                    "file_name_conflict",
                    f"같은 이름의 파일이 둘 이상 들어 있다: {row.filename}",
                    filename=row.filename,
                )
                continue
            names.add(folded)

    def _merge_env(
        self, stored: list[dict[str, Any]], incoming: list[EnvVarIn], problems: Problems
    ) -> list[dict[str, Any]]:
        existing = {item.name: item for item in self._stored_env(stored)}
        merged: list[dict[str, Any]] = []
        for index, item in enumerate(incoming):
            if item.value is None:
                old = existing.get(item.name)
                if old is None:
                    problems.add(
                        ("env_vars", index, "value"),
                        "env_value_required",
                        "새 환경변수는 값이 필요하다",
                    )
                    continue
                if old.secret and not item.secret:
                    # 비밀값을 일반 값으로 바꾸면 응답에 값이 보이게 되므로 값을 다시 받는다.
                    problems.add(
                        ("env_vars", index, "value"),
                        "env_value_required",
                        "비밀값을 일반 값으로 바꾸려면 값을 다시 입력한다",
                    )
                    continue
                encrypted = old.value_encrypted
            else:
                encrypted = self._cipher().encrypt(item.value)
            merged.append(
                StoredEnvVar(
                    name=item.name, value_encrypted=encrypted, secret=item.secret
                ).model_dump()
            )
        return merged

    def _stored_env(self, stored: list[dict[str, Any]]) -> list[StoredEnvVar]:
        items: list[StoredEnvVar] = []
        for raw in stored:
            try:
                items.append(StoredEnvVar.model_validate(raw))
            except ValidationError:
                log.warning("agent.env_var_invalid")
        return items

    def _env_out(self, stored: list[dict[str, Any]]) -> list[EnvVarOut]:
        out: list[EnvVarOut] = []
        for item in self._stored_env(stored):
            value: str | None = None
            if not item.secret:
                try:
                    value = self._cipher().decrypt(item.value_encrypted)
                except (ServiceError, CryptoError):
                    log.warning("agent.env_var_unreadable", name=item.name)
            out.append(EnvVarOut(name=item.name, secret=item.secret, value=value))
        return out

    def _decrypt_env(self, stored: list[dict[str, Any]]) -> dict[str, str]:
        env: dict[str, str] = {}
        for item in self._stored_env(stored):
            try:
                env[item.name] = self._cipher().decrypt(item.value_encrypted)
            except CryptoError:
                raise ServiceError(
                    500,
                    "env_decrypt_failed",
                    f"환경변수 {item.name}을(를) 복호화하지 못했다. ENCRYPTION_KEY가 바뀌었는지 "
                    "확인한다",
                ) from None
        return env

    def _version_out(self, version: AgentVersion) -> AgentVersionOut:
        return AgentVersionOut.model_validate(
            {
                **_config_dict(version),
                "id": version.id,
                "agent_id": version.agent_id,
                "version_number": version.version_number,
                "status": version.status,
                "env_vars": self._env_out(version.env_vars),
                "snapshot_id": version.snapshot_id,
                "published_at": version.published_at,
                "change_note": version.change_note,
                "created_at": version.created_at,
                "updated_at": version.updated_at,
            }
        )
