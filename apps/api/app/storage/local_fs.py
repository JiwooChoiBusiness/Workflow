import asyncio
import hashlib
import os
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from uuid import uuid4

from app.storage.base import (
    CHUNK_SIZE,
    StorageError,
    StorageLimitExceeded,
    StoredObject,
    check_key,
)


async def _file_chunks(source: Path, chunk_size: int) -> AsyncIterator[bytes]:
    reader = await asyncio.to_thread(source.open, "rb")
    try:
        while chunk := await asyncio.to_thread(reader.read, chunk_size):
            yield chunk
    finally:
        reader.close()


class LocalFsStorage:
    """data/storage 폴더 구현. 쓰기는 같은 폴더의 임시 파일에 받은 뒤 os.replace로 바꿔 넣어
    중간에 실패해도 반쯤 쓴 파일이 키 이름으로 남지 않는다."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def local_path(self, key: str) -> Path:
        parts = check_key(key)
        root = self.root.resolve()
        resolved = root.joinpath(*parts).resolve()
        # 저장소 안에 누가 링크·정션을 만들어 두었어도 밖으로 나가지 않는다.
        if not resolved.is_relative_to(root):
            raise StorageError("key_invalid", f"저장소 밖을 가리킨다: {key[:80]!r}")
        return resolved

    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int | None = None
    ) -> StoredObject:
        target = self.local_path(key)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.{uuid4().hex}.part")
        digest = hashlib.sha256()
        size = 0
        writer = await asyncio.to_thread(partial.open, "xb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise StorageLimitExceeded(max_bytes)
                digest.update(chunk)
                await asyncio.to_thread(writer.write, chunk)
            await asyncio.to_thread(writer.close)
            await asyncio.to_thread(os.replace, partial, target)
        except BaseException:
            writer.close()
            partial.unlink(missing_ok=True)
            raise
        return StoredObject(key=key, size_bytes=size, sha256=digest.hexdigest())

    async def write_file(
        self, key: str, source: Path, *, max_bytes: int | None = None
    ) -> StoredObject:
        return await self.write_stream(key, _file_chunks(source, CHUNK_SIZE), max_bytes=max_bytes)

    async def read_stream(self, key: str, *, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        path = self.local_path(key)
        try:
            reader = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError:
            raise StorageError("not_found", f"저장소에 없다: {key[:80]!r}") from None
        try:
            while chunk := await asyncio.to_thread(reader.read, chunk_size):
                yield chunk
        finally:
            reader.close()

    async def size(self, key: str) -> int | None:
        path = self.local_path(key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> bool:
        path = self.local_path(key)
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            return False
        return True
