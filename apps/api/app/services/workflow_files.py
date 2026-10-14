"""워크플로우 파일(2단계 설계 4.1 wf_file, 6.1).

미리보기 런의 파일 입력은 먼저 POST /workflows/{id}/files로 올리고 그 id를 start 파일 필드 값으로
준다. 런을 만들 때 id를 변수 풀의 파일 참조(FileRef, source=wf_file)로 바꾼다. 저장 경로는
storage의 wf-files/<workspace_id>/<file_id>다.
"""

import uuid
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

import structlog

from app.db.engine import Database
from app.db.models import WfFile
from app.db.models.enums import WfFileKind
from app.db.repositories.workflows import WfFileRepository, WorkflowRepository
from app.db.types import new_id, utcnow
from app.schemas.workflow import WfFileOut
from app.services.errors import ServiceError, not_found
from app.services.files import guess_mime, sanitize_filename
from app.storage.base import StorageBackend, StorageLimitExceeded, wf_file_key
from app.workflow.nodes.start import StartField
from app.workflow.variables import FileRef

log = structlog.get_logger("app.services.workflow_files")

MAX_WF_FILE_BYTES = 100 * 1024 * 1024
FILE_FIELD_TYPES = frozenset({"file", "file_list"})


def wf_file_out(row: WfFile) -> WfFileOut:
    return WfFileOut(
        id=row.id,
        kind=row.kind,  # pyright: ignore[reportArgumentType]
        workflow_id=row.workflow_id,
        wf_run_id=row.wf_run_id,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        created_at=row.created_at,
    )


def file_ref(row: WfFile) -> dict[str, Any]:
    return FileRef(
        file_id=str(row.id),
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        source="wf_file",
    ).model_dump(mode="json")


def _file_id(value: object) -> uuid.UUID | None:
    """올린 파일을 가리키는 값이면 그 id. 문자열 id와 {"file_id": id}를 받는다."""
    raw: object = value
    if isinstance(value, dict):
        item = cast(dict[str, Any], value)
        if set(item) != {"file_id"}:
            return None
        raw = item["file_id"]
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


class WorkflowFileService:
    def __init__(
        self, db: Database, storage: StorageBackend, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._db = db
        self._storage = storage
        self._clock = clock

    async def upload(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        filename: str | None,
        content_type: str | None,
        chunks: AsyncIterable[bytes],
    ) -> WfFileOut:
        name = sanitize_filename(filename)
        async with self._db.read() as session:
            if await WorkflowRepository(session, workspace_id).get_active(workflow_id) is None:
                raise not_found("워크플로우")
        file_id = new_id()
        key = wf_file_key(workspace_id, file_id)
        # 큰 파일을 받는 동안 DB 쓰기 잠금을 잡지 않도록 저장소에 먼저 쓴다.
        try:
            stored = await self._storage.write_stream(key, chunks, max_bytes=MAX_WF_FILE_BYTES)
        except StorageLimitExceeded:
            raise ServiceError(
                413, "file_too_large", f"파일은 {MAX_WF_FILE_BYTES // 2**20}MB 이하여야 한다"
            ) from None
        try:
            async with self._db.write() as session:
                if await WorkflowRepository(session, workspace_id).get_active(workflow_id) is None:
                    raise not_found("워크플로우")
                files = WfFileRepository(session, workspace_id)
                row = files.add(
                    WfFile(
                        id=file_id,
                        kind=WfFileKind.UPLOAD,
                        workflow_id=workflow_id,
                        filename=name,
                        mime=guess_mime(name, content_type),
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_key=key,
                        created_at=self._clock(),
                    )
                )
                await files.flush()
                out = wf_file_out(row)
        except BaseException:
            await self._storage.delete(key)
            raise
        log.info("wf_file.uploaded", workflow_id=str(workflow_id), file_id=str(file_id))
        return out

    async def resolve_inputs(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        fields: Sequence[StartField],
        inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """start 파일 필드에 온 파일 id를 FileRef로 바꾼다. 다른 필드는 그대로 둔다.

        이 워크플로우에 올린 파일만 받는다. 이미 FileRef 모양이면 저장소 키를 믿을 수 없으므로
        id로 다시 찾는다.
        """
        many = {
            field.name: field.type == "file_list"
            for field in fields
            if field.type in FILE_FIELD_TYPES
        }
        return await self.resolve_files(workspace_id, workflow_id, many, inputs)

    async def resolve_files(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        many: Mapping[str, bool],
        inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """many에 든 이름의 값(파일 id, 목록이면 id 배열)을 FileRef로 바꾼다.

        단일 노드 실행이 "<scope>.<name>" 키로도 쓴다.
        """
        wanted: dict[str, list[uuid.UUID]] = {}
        resolved = dict(inputs)
        for name, is_list in many.items():
            if inputs.get(name) is None:
                continue
            value = inputs[name]
            if is_list and not isinstance(value, list):
                raise ServiceError(
                    422,
                    "invalid_inputs",
                    "런 입력이 올바르지 않다",
                    details={name: "파일 목록이어야 한다"},
                )
            items: list[Any] = cast(list[Any], value) if is_list else [value]
            ids: list[uuid.UUID] = []
            for item in items:
                file_id = _file_id(item)
                if file_id is None and isinstance(item, dict):
                    file_id = _file_id({"file_id": cast(dict[str, Any], item).get("file_id")})
                if file_id is None:
                    raise ServiceError(
                        422,
                        "invalid_inputs",
                        "런 입력이 올바르지 않다",
                        details={name: "올린 파일의 id여야 한다"},
                    )
                ids.append(file_id)
            wanted[name] = ids
        if not wanted:
            return resolved
        async with self._db.read() as session:
            rows = await WfFileRepository(session, workspace_id).get_many(
                [file_id for ids in wanted.values() for file_id in ids]
            )
        by_id = {row.id: row for row in rows}
        for name, ids in wanted.items():
            refs: list[dict[str, Any]] = []
            for file_id in ids:
                row = by_id.get(file_id)
                if row is None or row.workflow_id != workflow_id or row.kind != WfFileKind.UPLOAD:
                    raise ServiceError(
                        422,
                        "file_not_found",
                        "이 워크플로우에 올린 파일이 아니다",
                        details={"field": name, "file_id": str(file_id)},
                    )
                refs.append(file_ref(row))
            resolved[name] = refs if many[name] else refs[0]
        return resolved
