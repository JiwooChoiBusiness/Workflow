import mimetypes
import os
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"

# Windows는 레지스트리의 확장자 매핑을 mimetypes에 덮어쓰는데, 일부 PC에서 .js가 text/plain으로
# 잡혀 있으면 브라우저가 모듈 스크립트 실행을 거부해 빈 화면이 된다.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

_NOT_BUILT_HTML = """<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<title>Workflow</title>
<p>화면이 아직 빌드되지 않았다. 저장소 루트에서 <code>corepack pnpm --dir apps/web build</code>를
실행한 뒤 서버를 다시 띄운다. 화면을 고치는 중이면 <code>corepack pnpm --dir apps/web dev</code>로
띄운 개발 서버(http://localhost:5173)를 쓴다.</p>
</html>
"""


class _HashedAssets(StaticFiles):
    # vite가 파일 이름에 내용 해시를 넣으므로 한 번 받은 파일은 다시 확인할 필요가 없다.
    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response


def _is_excluded(path: str) -> bool:
    return path == "api" or path.startswith("api/") or path in ("healthz", "readyz")


def mount_web(app: FastAPI, dist_dir: Path) -> None:
    # 다른 라우트를 모두 등록한 뒤 마지막에 불러야 한다. 이후 라우트는 이 폴백에 가려진다.
    dist_dir = dist_dir.resolve()
    index = dist_dir / "index.html"
    built = index.is_file()
    if built and (dist_dir / "assets").is_dir():
        app.mount("/assets", _HashedAssets(directory=dist_dir / "assets"), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> Response:
        if _is_excluded(path):
            raise HTTPException(status_code=404)
        if not built:
            return HTMLResponse(_NOT_BUILT_HTML)
        if path:
            try:
                candidate = (dist_dir / path).resolve()
            except (OSError, ValueError):
                raise HTTPException(status_code=404) from None
            if candidate != index:
                if candidate.is_relative_to(dist_dir) and candidate.is_file():
                    return FileResponse(candidate)
                # 확장자가 있으면 화면 라우트가 아니라 없는 파일이다.
                if "." in PurePosixPath(path).name:
                    raise HTTPException(status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
