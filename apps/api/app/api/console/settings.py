import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter

from app.auth.dependencies import CurrentAuth, SettingsDep
from app.sandbox.allowlist import RELOAD_CHECK_SEC, AllowlistRules
from app.schemas.settings import HttpAllowlistOut, HttpAllowlistRules
from app.services.errors import ServiceError

router = APIRouter(prefix="/settings", tags=["settings"])

MAX_SHOWN_BYTES = 256 * 1024
# 규칙 요약은 이만큼까지만 읽고 판단한다. 허용 목록 파일은 보통 몇백 바이트다.
MAX_PARSED_BYTES = 1024 * 1024


def read_allowlist(path: Path) -> HttpAllowlistOut:
    empty = HttpAllowlistRules(hosts=[], suffixes=[], allow_any=False)
    try:
        stat = path.stat()
        with path.open("rb") as file:
            raw = file.read(MAX_PARSED_BYTES)
    except FileNotFoundError:
        stat = None
        raw = b""
    except OSError as exc:
        raise ServiceError(
            500, "allowlist_unreadable", f"허용 목록 파일을 읽지 못했다: {exc}"
        ) from None
    # 서버(allowlist.py)와 같게 BOM이 있어도 읽는다. 깨진 바이트는 보이기만 하면 되므로 바꿔 넣는다.
    shown = raw[:MAX_SHOWN_BYTES].decode("utf-8-sig", errors="replace")
    parsed = AllowlistRules.parse(raw.decode("utf-8-sig", errors="replace").splitlines())
    return HttpAllowlistOut(
        path=str(path),
        exists=stat is not None,
        size_bytes=stat.st_size if stat is not None else None,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC) if stat is not None else None,
        content=shown,
        truncated=stat is not None and stat.st_size > MAX_SHOWN_BYTES,
        rules=(
            HttpAllowlistRules(
                hosts=sorted(parsed.exact),
                suffixes=list(parsed.suffixes),
                allow_any=parsed.allow_any,
            )
            if stat is not None
            else empty
        ),
        reload_interval_sec=RELOAD_CHECK_SEC,
    )


@router.get("/http-allowlist", response_model=HttpAllowlistOut)
async def http_allowlist(auth: CurrentAuth, settings: SettingsDep) -> HttpAllowlistOut:
    """http_request 허용 목록 파일의 경로·내용·수정 시각(읽기 전용).

    고치려면 파일을 직접 편집한다. 저장하면 2초 안에 반영된다(설계 3.9).
    """
    return await asyncio.to_thread(read_allowlist, settings.allowlist_file)
