"""에이전트 첨부 파일(설계 5.1 agent_file, 6.2).

업로드한 파일은 버전의 file_ids가 가리켜야 작업공간 agent-files/로 복사된다. 업로드만으로는 초안에
들어가지 않고 PUT .../draft로 넣는다(초안은 그 요청 하나로만 바뀐다). 어느 버전도 쓰지 않는 파일만
지울 수 있다.
"""

import mimetypes
import re
import unicodedata
import uuid
from collections.abc import AsyncIterable, Callable
from datetime import datetime

import structlog

from app.db.engine import Database
from app.db.models import AgentFile, AgentVersion
from app.db.repositories.agent_files import AgentFileRepository
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.agents import AgentRepository
from app.db.types import new_id, utcnow
from app.sandbox.paths import is_reserved_name
from app.schemas.agent_file import AgentFileList, AgentFileOut
from app.services.errors import ServiceError, not_found
from app.storage.base import StorageBackend, StorageLimitExceeded, agent_file_key

log = structlog.get_logger("app.services.files")

MAX_AGENT_FILE_BYTES = 100 * 1024 * 1024
# 작업공간 경로 agent-files/<이름>이 경로 검사(3.5)의 길이 한도 안에 넉넉히 들게 한다.
MAX_FILENAME_CHARS = 100
_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def sanitize_filename(raw: str | None) -> str:
    """업로드 파일 이름을 작업공간에서 그대로 쓸 수 있는 이름 하나로 만든다(11장 S9).

    폴더 부분은 버리고, Windows가 받지 않는 문자는 '_'로 바꾸고, 예약 이름(CON 등)은 앞에 '_'를
    붙인다. macOS가 보내는 NFD 한글은 NFC로 합친다.
    """
    name = unicodedata.normalize("NFC", raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _FORBIDDEN.sub("_", name).strip()
    # NTFS가 끝의 점·공백을 조용히 떼어 다른 이름이 되므로 미리 뗀다.
    name = name.rstrip(". ")
    if name in ("", ".", ".."):
        return "file"
    if is_reserved_name(name):
        name = f"_{name}"
    if len(name) > MAX_FILENAME_CHARS:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16 and stem:
            name = stem[: MAX_FILENAME_CHARS - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:MAX_FILENAME_CHARS].rstrip(". ")
    return name or "file"


def guess_mime(filename: str, declared: str | None) -> str:
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if _MIME.fullmatch(declared) and declared != "application/octet-stream":
        return declared
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _file_out(row: AgentFile, versions: list[AgentVersion]) -> AgentFileOut:
    key = str(row.id)
    return AgentFileOut(
        id=row.id,
        agent_id=row.agent_id,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        created_at=row.created_at,
        in_draft=any(v.version_number is None and key in v.file_ids for v in versions),
        published_versions=sorted(
            v.version_number for v in versions if v.version_number is not None and key in v.file_ids
        ),
    )


class AgentFileService:
    def __init__(
        self, db: Database, storage: StorageBackend, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._storage = storage
        self._clock = clock

    async def list_files(self, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> AgentFileList:
        async with self._db.read() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            rows = await AgentFileRepository(session, workspace_id).list_for_agent(agent_id)
            versions = await AgentVersionRepository(session, workspace_id).list_for_agent(agent_id)
        return AgentFileList(items=[_file_out(row, versions) for row in rows])

    async def upload(
        self,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        filename: str | None,
        content_type: str | None,
        chunks: AsyncIterable[bytes],
    ) -> AgentFileOut:
        name = sanitize_filename(filename)
        async with self._db.read() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
        file_id = new_id()
        key = agent_file_key(workspace_id, file_id)
        # 큰 파일을 받는 동안 DB 쓰기 잠금을 잡지 않도록 저장소에 먼저 쓴다.
        try:
            stored = await self._storage.write_stream(key, chunks, max_bytes=MAX_AGENT_FILE_BYTES)
        except StorageLimitExceeded:
            raise ServiceError(
                413,
                "file_too_large",
                f"첨부 파일은 {MAX_AGENT_FILE_BYTES // 2**20}MB 이하여야 한다",
            ) from None
        try:
            async with self._db.write() as session:
                if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                    raise not_found("에이전트")
                files = AgentFileRepository(session, workspace_id)
                row = files.add(
                    AgentFile(
                        id=file_id,
                        agent_id=agent_id,
                        filename=name,
                        mime=guess_mime(name, content_type),
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_key=key,
                    )
                )
                await files.flush()
                out = _file_out(row, [])
        except BaseException:
            await self._storage.delete(key)
            raise
        log.info("agent_file.uploaded", agent_id=str(agent_id), file_id=str(file_id))
        return out

    async def delete(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, file_id: uuid.UUID
    ) -> None:
        async with self._db.write() as session:
            if await AgentRepository(session, workspace_id).get_active(agent_id) is None:
                raise not_found("에이전트")
            files = AgentFileRepository(session, workspace_id)
            row = await files.get_for_agent(agent_id, file_id)
            if row is None:
                raise not_found("파일")
            versions = await AgentVersionRepository(session, workspace_id).list_for_agent(agent_id)
            usage = _file_out(row, versions)
            if usage.in_draft or usage.published_versions:
                raise ServiceError(
                    409,
                    "file_in_use",
                    "초안이나 발행 버전이 쓰는 파일은 지울 수 없다. 초안에서 빼고 저장한 뒤 지운다",
                    details={
                        "in_draft": usage.in_draft,
                        "published_versions": usage.published_versions,
                    },
                )
            await files.delete(row.id)
            key = row.storage_key
        # DB에서 먼저 지운다. 원본 삭제가 실패해도 가리키는 행이 없으니 정리 대상일 뿐이다.
        await self._storage.delete(key)
        log.info("agent_file.deleted", agent_id=str(agent_id), file_id=str(file_id))
