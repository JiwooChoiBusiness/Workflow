import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.storage.base import (
    StorageError,
    StorageLimitExceeded,
    agent_file_key,
    check_key,
    session_file_key,
    skill_package_key,
)
from app.storage.local_fs import LocalFsStorage


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


@pytest.fixture
def storage(tmp_path: Path) -> LocalFsStorage:
    return LocalFsStorage(tmp_path / "storage")


async def test_write_read_size_delete(storage: LocalFsStorage) -> None:
    key = agent_file_key(uuid.uuid4(), uuid.uuid4())

    stored = await storage.write_stream(key, chunks(b"hello ", "세계".encode()))

    data = "hello 세계".encode()
    assert stored.size_bytes == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert b"".join([chunk async for chunk in storage.read_stream(key, chunk_size=3)]) == data
    assert await storage.size(key) == len(data)
    assert await storage.delete(key) is True
    assert await storage.size(key) is None
    assert await storage.delete(key) is False


async def test_limit_leaves_nothing_behind(storage: LocalFsStorage) -> None:
    key = skill_package_key(uuid.uuid4(), uuid.uuid4())

    with pytest.raises(StorageLimitExceeded):
        await storage.write_stream(key, chunks(b"a" * 6, b"b" * 6), max_bytes=10)

    folder = storage.local_path(key).parent
    assert list(folder.iterdir()) == []


async def test_write_file_copies(storage: LocalFsStorage, tmp_path: Path) -> None:
    source = tmp_path / "out.csv"
    source.write_bytes(b"a,b\n1,2\n")
    key = session_file_key(uuid.uuid4(), "outputs/결과.csv")

    stored = await storage.write_file(key, source)

    assert storage.local_path(key).read_bytes() == b"a,b\n1,2\n"
    assert stored.size_bytes == 8


async def test_missing_key_reads_raise(storage: LocalFsStorage) -> None:
    with pytest.raises(StorageError) as info:
        [chunk async for chunk in storage.read_stream("uploads/x/y")]
    assert info.value.code == "not_found"


@pytest.mark.parametrize(
    "key",
    ["", "../x", "a/../b", "/abs", "a//b", "a/./b", "a\\b", "C:/x", "a/b\x00", "x" * 401],
)
def test_keys_must_stay_inside(key: str) -> None:
    with pytest.raises(StorageError):
        check_key(key)


def test_keys_have_fixed_shapes() -> None:
    workspace_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    item_id = uuid.UUID("00000000-0000-7000-8000-000000000002")

    assert agent_file_key(workspace_id, item_id) == f"agent-files/{workspace_id}/{item_id}"
    assert skill_package_key(workspace_id, item_id) == f"skills/{workspace_id}/{item_id}.zip"
    assert check_key(agent_file_key(workspace_id, item_id))[0] == "agent-files"


def test_session_file_key_is_run_and_relative_path() -> None:
    # 세션 id를 넣지 않아 Windows 경로 한계(260자)에 덜 닿는다(12.2 M6 이후 확정).
    run_id = uuid.UUID("00000000-0000-7000-8000-00000000000b")

    key = session_file_key(run_id, "outputs/표/결과.csv")

    assert key == f"session-files/{run_id}/outputs/표/결과.csv"
    assert check_key(key) == ("session-files", str(run_id), "outputs", "표", "결과.csv")
