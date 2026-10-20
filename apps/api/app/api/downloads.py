"""파일 내려보내기(설계 11장 S9).

항상 attachment로 내려보내고 nosniff를 붙인다. 브라우저가 열면 스크립트가 도는 형식(HTML, SVG)은
application/octet-stream으로 바꾼다.
"""

import unicodedata
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.storage.base import StorageBackend

_ACTIVE_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"}
)


def safe_media_type(mime: str) -> str:
    media = mime.split(";", 1)[0].strip().lower()
    if not media or media in _ACTIVE_TYPES or media.endswith("+xml"):
        return "application/octet-stream"
    # 텍스트는 문자 집합을 붙여 둔 그대로 넘긴다(한글 출력이 깨지지 않게).
    return mime.strip() if media.startswith("text/") else media


def content_disposition(filename: str, fallback: str | None = None) -> str:
    # 한글 이름은 filename*로 주고, filename에는 ASCII로 바꾼 이름을 둔다(RFC 6266). 한글만인
    # 이름은 ASCII로 바꾸면 거의 남지 않으므로 부르는 쪽이 fallback(slug 등)을 줄 수 있다.
    source = fallback or filename
    ascii_name = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    ascii_name = "".join(char for char in ascii_name if char.isprintable() and char not in '"\\')
    ascii_name = ascii_name.strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def download_headers(filename: str, fallback: str | None = None) -> dict[str, str]:
    return {
        "Content-Disposition": content_disposition(filename, fallback),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }


def stored_file_response(
    storage: StorageBackend, key: str, *, filename: str, mime: str
) -> StreamingResponse:
    return StreamingResponse(
        storage.read_stream(key),
        media_type=safe_media_type(mime),
        headers=download_headers(filename),
    )


async def stored_download(
    storage: StorageBackend, key: str, *, filename: str, mime: str
) -> Response:
    """저장소의 파일을 내려보낸다. 행은 있는데 파일이 없으면 404 file_missing."""
    if await storage.size(key) is None:
        return JSONResponse(
            {"error": {"code": "file_missing", "message": "저장소에 파일이 없다"}},
            status_code=404,
        )
    return stored_file_response(storage, key, filename=filename, mime=mime)


async def _text_chunks(text: str) -> AsyncIterator[bytes]:
    yield text.encode("utf-8")


def text_download_response(text: str, *, filename: str) -> StreamingResponse:
    return StreamingResponse(
        _text_chunks(text),
        media_type="text/plain; charset=utf-8",
        headers=download_headers(filename),
    )
