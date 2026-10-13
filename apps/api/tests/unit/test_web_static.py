from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.web_static import ASSET_CACHE_CONTROL, mount_web

INDEX_HTML = "<!doctype html><div id=root></div>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-3f9a1c.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    return root


def client_for(dist_dir: Path) -> AsyncClient:
    app = FastAPI()
    mount_web(app, dist_dir)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8500")


async def test_screen_routes_fall_back_to_index(dist: Path) -> None:
    async with client_for(dist) as client:
        for path in ("/", "/agents/abc", "/logs/sessions/x/", "/index.html"):
            response = await client.get(path)

            assert response.status_code == 200, path
            assert response.text == INDEX_HTML, path
            assert response.headers["cache-control"] == "no-cache", path


async def test_hashed_assets_are_cached_forever(dist: Path) -> None:
    async with client_for(dist) as client:
        response = await client.get("/assets/index-3f9a1c.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert response.headers["content-type"].startswith("text/javascript")


async def test_root_files_are_served_and_missing_files_are_404(dist: Path) -> None:
    async with client_for(dist) as client:
        favicon = await client.get("/favicon.svg")
        missing_root_file = await client.get("/robots.txt")
        missing_asset = await client.get("/assets/index-000000.js")

    assert favicon.status_code == 200
    assert favicon.text == "<svg/>"
    assert missing_root_file.status_code == 404
    assert missing_asset.status_code == 404


async def test_api_and_health_paths_are_not_screen_routes(dist: Path) -> None:
    async with client_for(dist) as client:
        for path in ("/api", "/api/nope", "/api/console/agents", "/healthz", "/readyz"):
            response = await client.get(path)

            assert response.status_code == 404, path
            assert response.json() == {"detail": "Not Found"}, path


@pytest.mark.parametrize(
    "path", ["/%2e%2e/secret.txt", "/..%5Csecret.txt", "/a/%2e%2e/%2e%2e/secret.txt"]
)
async def test_paths_cannot_leave_dist(dist: Path, path: str) -> None:
    async with client_for(dist) as client:
        response = await client.get(path)

    assert "secret" not in response.text
    assert response.status_code in (200, 404)


async def test_missing_build_shows_hint(tmp_path: Path) -> None:
    async with client_for(tmp_path / "not-built") as client:
        page = await client.get("/agents/abc")
        api = await client.get("/api/nope")

    assert page.status_code == 200
    assert "pnpm --dir apps/web build" in page.text
    assert api.status_code == 404
