"""platform backup(설계 2.10, M10.1 (1), S16)."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app import backup, cli, config, sysinfo
from app.config import Settings
from app.logsetup import configure_logging
from app.main import create_app
from app.sandbox.local_workspace import DELETED_PREFIX

FETCH = {"X-Requested-With": "fetch"}
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "updated_at",
)
MANIFEST_KEYS = {
    "created_at",
    "source_data_dir",
    "alembic_revision",
    "with_workspaces",
    "files",
    "bytes",
    "quick_check",
    "encryption_key_fingerprint",
}
# 실행할 때 만든 가짜 키. 백업 어디에도 나오면 안 된다.
ENCRYPTION_KEY = base64.b64encode(secrets.token_bytes(32)).decode()
E2B_KEY = "e2b_" + secrets.token_hex(16)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # 사용자 서버(8500)가 떠 있어도 테스트가 "서버 실행 중"으로 보지 않게 빈 포트를 쓴다.
    return settings.model_copy(
        update={
            "encryption_key": SecretStr(ENCRYPTION_KEY),
            "e2b_api_key": SecretStr(E2B_KEY),
            "server_port": free_port(),
        }
    )


@asynccontextmanager
async def running(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        configure_logging("INFO")


def backup_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def plain_data_dir(root: Path) -> Path:
    """서버 없이 쓰는 최소 DATA_DIR. 서버처럼 WAL 모드 DB를 둔다."""
    data = root / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "app.db")) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE note (body TEXT)")
        conn.execute("INSERT INTO note VALUES ('hello')")
        conn.commit()
    return data


def plain_settings(data: Path) -> Settings:
    return Settings.model_validate({"data_dir": data, "server_port": free_port()})


async def test_backup_of_a_running_server_restores_agents_sessions_and_files(
    logged_in: AsyncClient, settings: Settings, tmp_path: Path, login: Callable[..., Any]
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "백업 대상"})).json()
    content = "이름,값\n가,1\n".encode()
    uploaded = await logged_in.post(
        f"/api/console/agents/{agent['id']}/files",
        files={"file": ("데이터.csv", content, "text/csv")},
        headers=FETCH,
    )
    assert uploaded.status_code == 201, uploaded.text
    session = await logged_in.post(f"/api/console/agents/{agent['id']}/preview/sessions", json={})
    assert session.status_code == 201, session.text
    # 저장소가 쓰다 만 임시 파일과 백업에서 빼는 폴더.
    partial = settings.data_dir / "storage" / "uploads" / "x.0123abcd.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"half")
    (settings.data_dir / "workspaces" / "some-session").mkdir(parents=True)
    out = tmp_path / "backup"

    result = await asyncio.to_thread(backup.create_backup, settings, out=out)

    assert result.path == out.resolve() and result.healthy
    assert not out.with_name("backup.part").exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == result.manifest
    assert set(manifest) == MANIFEST_KEYS
    with closing(sqlite3.connect(settings.data_dir / "app.db")) as source:
        (head,) = source.execute("SELECT version_num FROM alembic_version").fetchone()
    assert manifest["alembic_revision"] == head
    assert manifest["quick_check"] == "ok"
    assert manifest["with_workspaces"] is False
    assert manifest["source_data_dir"] == str(settings.data_dir)
    assert manifest["encryption_key_fingerprint"] == sysinfo.encryption_key_fingerprint(settings)
    files = backup_files(out)
    del files["manifest.json"]
    assert (manifest["files"], manifest["bytes"]) == (len(files), sum(map(len, files.values())))
    # 서버가 떠 있는 동안 만든 복사본도 -wal·-shm 없이 파일 하나다.
    assert {"app.db", "allowlist.txt"} <= set(files)
    assert not any(name.startswith("app.db-") for name in files)
    assert not any(name.endswith(".part") for name in files)
    top = {name.split("/", 1)[0] for name in files}
    assert top.isdisjoint({"logs", "cache", "workspaces"})
    file_id = uploaded.json()["id"]
    (stored,) = [name for name in files if name.endswith(f"/{file_id}")]
    assert stored.startswith("storage/agent-files/") and files[stored] == content

    # A15: 그 폴더를 DATA_DIR로 띄우면 그대로 보인다.
    async with running(settings.model_copy(update={"data_dir": out})) as restored:
        async with AsyncClient(
            transport=ASGITransport(app=restored), base_url="http://127.0.0.1:8500"
        ) as client:
            assert (await login(client)).status_code == 200
            agents = (await client.get("/api/console/agents")).json()["items"]
            listed = (await client.get(f"/api/console/agents/{agent['id']}/files")).json()
            opened = await client.get(f"/api/console/sessions/{session.json()['id']}")

    assert [item["id"] for item in agents] == [agent["id"]]
    assert [item["sha256"] for item in listed["items"]] == [hashlib.sha256(content).hexdigest()]
    assert opened.status_code == 200


async def test_backup_carries_no_key_material(
    logged_in: AsyncClient, settings: Settings, tmp_path: Path
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "비밀"})).json()
    draft = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body["env_vars"] = [
        {"name": "API_TOKEN", "value": "tok-" + secrets.token_hex(8), "secret": True}
    ]
    saved = await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    keys = settings.secret_values()
    assert ENCRYPTION_KEY in keys and E2B_KEY in keys
    # 누가 .env를 데이터 폴더에 두었거나 예전 로그에 키가 남아 있어도 백업에는 들어가지 않는다.
    (settings.data_dir / ".env").write_text(
        f"ENCRYPTION_KEY={ENCRYPTION_KEY}\nE2B_API_KEY={E2B_KEY}\n", encoding="utf-8"
    )
    (settings.data_dir / "logs" / "old.log").write_text(" ".join(keys), encoding="utf-8")
    out = tmp_path / "backup"

    result = await asyncio.to_thread(backup.create_backup, settings, out=out)

    files = backup_files(out)
    assert ".env" not in files
    assert not any(name.startswith("logs/") for name in files)
    raw_key = base64.b64decode(ENCRYPTION_KEY)
    for name, data in files.items():
        for value in keys:
            assert value.encode() not in data, name
        assert raw_key not in data, name
    # 키 대신 복구 때 맞춰 볼 fingerprint만 남는다.
    assert result.manifest["encryption_key_fingerprint"] == hashlib.sha256(raw_key).hexdigest()[:8]


def test_keep_prunes_only_timestamped_backups_in_the_default_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = plain_settings(plain_data_dir(tmp_path))
    root = tmp_path / "backups"
    monkeypatch.setattr(backup, "DEFAULT_ROOT", root)
    old = [root / f"2026010{day}-000000" for day in range(1, 6)]
    for path in old:
        path.mkdir(parents=True)
        (path / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "20250101-000000").mkdir()
    (root / "before-upgrade").mkdir()
    (root / "before-upgrade" / "manifest.json").write_text("{}", encoding="utf-8")

    result = backup.create_backup(settings, keep=3, now=datetime(2026, 10, 13, 9, 30, 0))

    assert result.path == root / "20261013-093000"
    assert result.pruned == old[:3] and result.prune_failed == []
    assert sorted(path.name for path in root.iterdir()) == [
        "20250101-000000",
        "20260104-000000",
        "20260105-000000",
        "20261013-093000",
        "before-upgrade",
    ]

    elsewhere = backup.create_backup(settings, out=tmp_path / "manual", keep=1)

    assert elsewhere.pruned == [] and len(list(root.iterdir())) == 5


def test_failed_backup_leaves_no_part_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = plain_data_dir(tmp_path)
    (data / "storage").mkdir()
    out = tmp_path / "backup"

    def broken_copy(*args: Any) -> None:
        assert (tmp_path / "backup.part" / "app.db").is_file()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup, "_copy_tree", broken_copy)

    with pytest.raises(OSError, match="No space left"):
        backup.create_backup(plain_settings(data), out=out)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["data"]


def test_existing_or_nested_targets_are_refused(tmp_path: Path) -> None:
    data = plain_data_dir(tmp_path)
    settings = plain_settings(data)
    (tmp_path / "taken").mkdir()
    (tmp_path / "stale.part").mkdir()

    for out, message in [
        (tmp_path / "taken", "이미 있다"),
        (tmp_path / "stale", "끊긴 백업"),
        (data / "storage" / "inside", "storage"),
    ]:
        with pytest.raises(backup.BackupError, match=message):
            backup.create_backup(settings, out=out)
    with pytest.raises(backup.BackupError, match="workspaces"):
        backup.create_backup(settings, out=data / "workspaces" / "x", with_workspaces=True)
    with pytest.raises(backup.BackupError, match="app.db"):
        backup.create_backup(plain_settings(tmp_path / "empty"), out=tmp_path / "nothing")

    # 복사하지 않는 곳이면 DATA_DIR 안이라도 된다(완료 기준의 --out 예).
    assert backup.create_backup(settings, out=data / "backup").healthy


def make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_links_partial_writes_and_deleted_workspaces_are_left_out(tmp_path: Path) -> None:
    data = plain_data_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("PC의 다른 파일", encoding="utf-8")
    results = data / "storage" / "session-files" / "s1"
    results.mkdir(parents=True)
    (results / "result.csv").write_text("a,b\n", encoding="utf-8")
    (results / "result.csv.0f0f.part").write_text("a,", encoding="utf-8")
    workspace = data / "workspaces" / "s1"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "chart.png").write_bytes(b"png")
    # 모델이 작업공간 밖을 가리키는 정션·링크를 만들어 두었을 수 있다.
    make_dir_link(workspace / "escape", outside)
    deleted = data / "workspaces" / f"{DELETED_PREFIX}1700000000-s0"
    deleted.mkdir()
    (deleted / "left.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "backup"

    result = backup.create_backup(plain_settings(data), out=out, with_workspaces=True)

    assert sorted(backup_files(out)) == [
        "app.db",
        "manifest.json",
        "storage/session-files/s1/result.csv",
        "workspaces/s1/outputs/chart.png",
    ]
    assert result.links_skipped == 1
    assert result.manifest["with_workspaces"] is True
    assert backup.DELETED_PREFIX == DELETED_PREFIX


@pytest.mark.skipif(sys.platform != "win32", reason="경로 길이 제한은 Windows 문제다")
def test_paths_longer_than_260_characters_are_copied(tmp_path: Path) -> None:
    data = plain_data_dir(tmp_path)
    deep = data / "storage" / "session-files" / ("d" * 100) / ("e" * 100)
    out = tmp_path / "backup"
    try:
        os.makedirs(sysinfo.long_path(deep))
        with open(sysinfo.long_path(deep / "result.txt"), "w", encoding="utf-8") as file:
            file.write("깊은 결과")

        result = backup.create_backup(plain_settings(data), out=out)

        copied = out / "storage" / "session-files" / ("d" * 100) / ("e" * 100) / "result.txt"
        assert len(str(copied)) > 260
        with open(sysinfo.long_path(copied), encoding="utf-8") as file:
            assert file.read() == "깊은 결과"
        assert result.manifest["files"] == 2
    finally:
        # pytest의 임시 폴더 정리는 260자를 넘는 경로를 지우지 못한다.
        for root in (data, out):
            shutil.rmtree(sysinfo.long_path(root), ignore_errors=True)


def test_cli_prints_the_result_and_warns_when_the_server_is_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = plain_data_dir(tmp_path)
    out = tmp_path / "backup"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        settings = Settings.model_validate({"data_dir": data, "server_port": port})
        monkeypatch.setattr(config, "get_settings", lambda: settings)

        code = cli.main(["backup", "--out", str(out), "--keep", "2"])

    captured = capsys.readouterr()
    assert code == 0
    assert f"127.0.0.1:{port}에 서버가 떠 있다" in captured.err
    assert "--out을 주면 --keep으로 정리하지 않는다" in captured.err
    first, second = captured.out.splitlines()
    assert first.startswith(f"backup: {out.resolve()} (파일 1개, ")
    assert second.endswith("암호화 키 fingerprint 없음")

    with pytest.raises(SystemExit) as invalid:
        cli.main(["backup", "--keep", "0"])
    assert invalid.value.code == 2
