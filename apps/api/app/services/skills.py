"""라이브러리·임베디드 스킬과 버전(설계 4.8, 5.1, 6.2).

업로드한 zip은 app/skills/package.py로 검사한 뒤 원본 그대로 data/storage/skills/에 둔다.
스킬을 지우면(deleted_at) 목록과 새 참조에서 빠지고 slug를 다시 쓸 수 있지만, 버전과 패키지는
남아 이미 발행한 버전이 계속 쓴다. 초안에 남은 참조는 다음 저장 때 skill_deleted로 걸러진다.
"""

import asyncio
import codecs
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import Database
from app.db.models import Skill, SkillVersion
from app.db.models.enums import SkillScope
from app.db.repositories.agents import AgentRepository
from app.db.repositories.skill_versions import SkillVersionRepository
from app.db.repositories.skills import SkillRepository
from app.db.types import new_id, utcnow
from app.schemas.skill import (
    SkillDetailOut,
    SkillList,
    SkillManifest,
    SkillOut,
    SkillVersionOut,
    SkillVersionSummary,
)
from app.services.agents import MAX_EMBEDDED_SKILLS
from app.services.errors import ServiceError, not_found
from app.skills.package import (
    MAX_PACKAGE_BYTES,
    InspectedPackage,
    SkillPackageError,
    find_member,
    inspect_package,
)
from app.storage.base import (
    CHUNK_SIZE,
    StorageBackend,
    StorageLimitExceeded,
    StoredObject,
    skill_package_key,
)

if TYPE_CHECKING:
    import zipfile

log = structlog.get_logger("app.services.skills")

_SNIFF_BYTES = 8192


@dataclass
class PackageMember:
    path: str
    size: int
    # UTF-8로 읽히고 NUL이 없으면 텍스트로 본다. 화면은 텍스트만 바로 보여 준다.
    is_text: bool
    chunks: AsyncIterator[bytes]


@dataclass(frozen=True)
class _StoredPackage:
    version_id: uuid.UUID
    stored: StoredObject
    package: InspectedPackage


def _looks_text(head: bytes, *, complete: bool) -> bool:
    if b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=complete)
    except UnicodeDecodeError:
        return False
    return True


def _open_member(
    archive_path: Path, rel: str
) -> "tuple[zipfile.ZipFile, zipfile.ZipInfo, IO[bytes]] | None":
    import zipfile

    archive = zipfile.ZipFile(archive_path)
    try:
        info = find_member(archive, rel)
        if info is None:
            archive.close()
            return None
        return archive, info, archive.open(info)
    except BaseException:
        archive.close()
        raise


async def _member_chunks(
    archive: "zipfile.ZipFile", reader: IO[bytes], head: bytes
) -> AsyncIterator[bytes]:
    try:
        if head:
            yield head
        while chunk := await asyncio.to_thread(reader.read, CHUNK_SIZE):
            yield chunk
    finally:
        reader.close()
        archive.close()


def _version_summary(version: SkillVersion) -> SkillVersionSummary:
    return SkillVersionSummary(
        id=version.id,
        version_number=version.version_number,
        size_bytes=version.size_bytes,
        sha256=version.sha256,
        created_at=version.created_at,
    )


def _version_out(version: SkillVersion) -> SkillVersionOut:
    return SkillVersionOut(
        id=version.id,
        skill_id=version.skill_id,
        version_number=version.version_number,
        size_bytes=version.size_bytes,
        sha256=version.sha256,
        created_at=version.created_at,
        manifest=SkillManifest.model_validate(version.manifest),
        skill_md=version.skill_md,
    )


def _skill_out(skill: Skill, versions: list[SkillVersion]) -> SkillOut:
    """versions는 그 스킬의 버전 전부(새 번호부터)."""
    current = next((v for v in versions if v.id == skill.current_version_id), None)
    return SkillOut(
        id=skill.id,
        scope="embedded" if skill.scope == SkillScope.EMBEDDED else "library",
        agent_id=skill.agent_id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        current_version_id=skill.current_version_id,
        current_version_number=current.version_number if current else None,
        versions=[_version_summary(version) for version in versions],
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _detail(skill: Skill, versions: list[SkillVersion]) -> SkillDetailOut:
    current = next((v for v in versions if v.id == skill.current_version_id), None)
    return SkillDetailOut(
        **_skill_out(skill, versions).model_dump(),
        current_version=_version_out(current) if current else None,
    )


def _skill_exists(skill: Skill | None, slug: str) -> ServiceError:
    details = {"slug": slug, "skill_id": str(skill.id)} if skill else {"slug": slug}
    return ServiceError(
        409,
        "skill_exists",
        f"같은 이름의 스킬이 이미 있다: {slug}. 새 버전은 그 스킬의 versions로 올린다",
        details=details,
    )


class SkillService:
    def __init__(
        self, db: Database, storage: StorageBackend, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._storage = storage
        self._clock = clock

    async def list_library(self, workspace_id: uuid.UUID) -> SkillList:
        async with self._db.read() as session:
            skills = await SkillRepository(session, workspace_id).list_library()
            return await self._list(session, workspace_id, skills)

    async def list_embedded(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> SkillList:
        async with self._db.read() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            skills = await SkillRepository(session, workspace_id).list_embedded(agent_id)
            return await self._list(session, workspace_id, skills)

    async def get(self, workspace_id: uuid.UUID, skill_id: uuid.UUID) -> SkillDetailOut:
        async with self._db.read() as session:
            skill = await self._skill(session, workspace_id, skill_id)
            versions = await SkillVersionRepository(session, workspace_id).list_for_skill(skill.id)
        return _detail(skill, versions)

    async def create(
        self,
        workspace_id: uuid.UUID,
        chunks: AsyncIterable[bytes],
        *,
        agent_id: uuid.UUID | None = None,
    ) -> SkillDetailOut:
        """agent_id가 있으면 그 에이전트의 임베디드 스킬, 없으면 라이브러리 스킬."""
        scope = SkillScope.EMBEDDED if agent_id is not None else SkillScope.LIBRARY
        if agent_id is not None:
            async with self._db.read() as session:
                await self._check_embedded_room(session, workspace_id, agent_id)
        uploaded = await self._store(workspace_id, chunks)
        manifest = uploaded.package.manifest
        try:
            async with self._db.write() as session:
                if agent_id is not None:
                    await self._check_embedded_room(session, workspace_id, agent_id)
                skills = SkillRepository(session, workspace_id)
                existing = await skills.get_active_by_slug(scope, agent_id, manifest.name)
                if existing is not None:
                    raise _skill_exists(existing, manifest.name)
                skill = skills.add(
                    Skill(
                        id=new_id(),
                        scope=scope,
                        agent_id=agent_id,
                        slug=manifest.name,
                        name=manifest.name,
                        description=manifest.description,
                        current_version_id=uploaded.version_id,
                    )
                )
                await skills.flush()
                version = self._add_version(session, workspace_id, skill, uploaded, number=1)
                await skills.flush()
                out = _detail(skill, [version])
        except IntegrityError:
            await self._storage.delete(uploaded.stored.key)
            raise _skill_exists(None, manifest.name) from None
        except BaseException:
            await self._storage.delete(uploaded.stored.key)
            raise
        log.info("skill.created", skill_id=str(out.id), slug=out.slug, scope=out.scope)
        return out

    async def add_version(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, chunks: AsyncIterable[bytes]
    ) -> SkillVersionOut:
        async with self._db.read() as session:
            await self._skill(session, workspace_id, skill_id)
        uploaded = await self._store(workspace_id, chunks)
        manifest = uploaded.package.manifest
        try:
            async with self._db.write() as session:
                skill = await self._skill(session, workspace_id, skill_id)
                if manifest.name != skill.slug:
                    raise ServiceError(
                        422,
                        "skill_name_mismatch",
                        f"패키지의 name({manifest.name})이 스킬 이름({skill.slug})과 다르다. "
                        "이름을 바꾸려면 새 스킬로 올린다",
                    )
                number = await SkillVersionRepository(session, workspace_id).next_version_number(
                    skill.id
                )
                version = self._add_version(session, workspace_id, skill, uploaded, number=number)
                skill.current_version_id = version.id
                skill.description = manifest.description
                await SkillRepository(session, workspace_id).flush()
                out = _version_out(version)
        except BaseException:
            await self._storage.delete(uploaded.stored.key)
            raise
        log.info("skill.version_added", skill_id=str(skill_id), version=out.version_number)
        return out

    async def delete(self, workspace_id: uuid.UUID, skill_id: uuid.UUID) -> None:
        async with self._db.write() as session:
            skill = await self._skill(session, workspace_id, skill_id)
            skill.deleted_at = self._clock()
        log.info("skill.deleted", skill_id=str(skill_id))

    async def open_file(
        self, workspace_id: uuid.UUID, skill_id: uuid.UUID, version_id: uuid.UUID, path: str
    ) -> PackageMember:
        """스킬 버전 패키지 안의 파일 하나. path는 스킬 루트(SKILL.md 폴더) 기준이다."""
        async with self._db.read() as session:
            skill = await self._skill(session, workspace_id, skill_id)
            version = await SkillVersionRepository(session, workspace_id).get_for_skill(
                skill.id, version_id
            )
        if version is None:
            raise not_found("스킬 버전")
        archive_path = self._storage.local_path(version.storage_key)
        try:
            opened = await asyncio.to_thread(_open_member, archive_path, path)
        except FileNotFoundError:
            raise ServiceError(410, "package_missing", "스킬 패키지 원본이 저장소에 없다") from None
        if opened is None:
            raise not_found("파일")
        archive, info, reader = opened
        try:
            head = await asyncio.to_thread(reader.read, _SNIFF_BYTES)
        except BaseException:
            reader.close()
            archive.close()
            raise
        return PackageMember(
            path=path,
            size=info.file_size,
            is_text=_looks_text(head, complete=len(head) >= info.file_size),
            chunks=_member_chunks(archive, reader, head),
        )

    async def _list(
        self, session: AsyncSession, workspace_id: uuid.UUID, skills: list[Skill]
    ) -> SkillList:
        versions = await SkillVersionRepository(session, workspace_id).list_for_skills(
            [skill.id for skill in skills]
        )
        by_skill: dict[uuid.UUID, list[SkillVersion]] = {}
        for version in versions:
            by_skill.setdefault(version.skill_id, []).append(version)
        return SkillList(items=[_skill_out(skill, by_skill.get(skill.id, [])) for skill in skills])

    async def _skill(
        self, session: AsyncSession, workspace_id: uuid.UUID, skill_id: uuid.UUID
    ) -> Skill:
        skill = await SkillRepository(session, workspace_id).get_active(skill_id)
        # 지운 에이전트의 임베디드 스킬은 에이전트와 함께 사라진 것으로 본다.
        if skill is not None and skill.agent_id is not None:
            if await AgentRepository(session, workspace_id).get_active(skill.agent_id) is None:
                skill = None
        if skill is None:
            raise not_found("스킬")
        return skill

    async def _check_embedded_room(
        self, session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID
    ) -> None:
        if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
            raise not_found("에이전트")
        count = await SkillRepository(session, workspace_id).count_embedded(agent_id)
        if count >= MAX_EMBEDDED_SKILLS:
            raise ServiceError(
                409,
                "skill_limit_reached",
                f"임베디드 스킬은 에이전트당 {MAX_EMBEDDED_SKILLS}개까지다",
                details={"max": MAX_EMBEDDED_SKILLS},
            )

    async def _store(self, workspace_id: uuid.UUID, chunks: AsyncIterable[bytes]) -> _StoredPackage:
        version_id = new_id()
        key = skill_package_key(workspace_id, version_id)
        try:
            stored = await self._storage.write_stream(key, chunks, max_bytes=MAX_PACKAGE_BYTES)
        except StorageLimitExceeded:
            raise ServiceError(
                413,
                "package_too_large",
                f"스킬 패키지는 {MAX_PACKAGE_BYTES // 2**20}MB 이하여야 한다",
            ) from None
        try:
            package = await asyncio.to_thread(inspect_package, self._storage.local_path(key))
        except SkillPackageError as exc:
            await self._storage.delete(key)
            raise ServiceError(exc.status, exc.code, exc.message) from None
        except BaseException:
            await self._storage.delete(key)
            raise
        return _StoredPackage(version_id=version_id, stored=stored, package=package)

    def _add_version(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        skill: Skill,
        uploaded: _StoredPackage,
        *,
        number: int,
    ) -> SkillVersion:
        return SkillVersionRepository(session, workspace_id).add(
            SkillVersion(
                id=uploaded.version_id,
                skill_id=skill.id,
                version_number=number,
                manifest=uploaded.package.manifest.model_dump(mode="json"),
                storage_key=uploaded.stored.key,
                size_bytes=uploaded.stored.size_bytes,
                sha256=uploaded.stored.sha256,
                skill_md=uploaded.package.skill_md,
            )
        )
