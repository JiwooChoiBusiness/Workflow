"""http_request 경로. DNS와 연결은 가짜(해석 표 + httpx.MockTransport)라 네트워크를 쓰지 않는다."""

import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from app.sandbox.allowlist import Allowlist
from app.sandbox.base import HttpSpec, SandboxError
from app.sandbox.http_client import (
    MAX_REDIRECTS,
    REQUEST_BODY_LIMIT,
    TEXT_LIMIT,
    USER_AGENT,
    HttpClient,
    is_public_address,
    is_textual,
    mask_headers,
)

PUBLIC = "93.184.216.34"
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


def make_client(
    tmp_path: Path,
    handler: Handler,
    *,
    entries: list[str] | None = None,
    dns: dict[str, list[str]] | None = None,
    max_body: int = 5 * 1024 * 1024,
) -> HttpClient:
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("\n".join(entries or ["example.com", ".example.com"]), encoding="utf-8")
    table = dns if dns is not None else {"example.com": [PUBLIC], "cdn.example.com": [PUBLIC]}

    async def resolver(host: str, port: int) -> list[str]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return table[host]

    return HttpClient(
        Allowlist(allowlist),
        max_body_bytes=max_body,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
async def client(tmp_path: Path, seen: list[httpx.Request]) -> AsyncIterator[HttpClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, text="안녕", headers={"content-type": "text/plain; charset=utf-8"}
        )

    instance = make_client(tmp_path, handler)
    yield instance
    await instance.aclose()


def spec(url: str, method: str = "GET", **fields: object) -> HttpSpec:
    return HttpSpec.model_validate({"exec_id": "call_1", "method": method, "url": url} | fields)


async def error_code(client: HttpClient, workspace: Path, request: HttpSpec) -> str:
    with pytest.raises(SandboxError) as info:
        await client.request(request, workspace_root=workspace)
    return info.value.code


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/x", "file:///etc/passwd", "https://user:pw@example.com/", "http:///nohost"],
)
async def test_bad_urls(client: HttpClient, workspace: Path, url: str) -> None:
    assert await error_code(client, workspace, spec(url)) == "http_url_invalid"


@pytest.mark.parametrize(
    "url", ["http://localhost:8500/healthz", "http://api.localhost/", "http://printer.local/"]
)
async def test_local_names_are_refused_without_resolution(
    tmp_path: Path, workspace: Path, url: str
) -> None:
    client = make_client(tmp_path, lambda r: httpx.Response(200), entries=["*"], dns={})

    assert await error_code(client, workspace, spec(url)) == "http_address_blocked"
    await client.aclose()


async def test_hosts_outside_the_allowlist(client: HttpClient, workspace: Path) -> None:
    assert (
        await error_code(client, workspace, spec("https://evil.test/")) == "http_host_not_allowed"
    )
    # .example.com은 하위 도메인만, example.com은 따로 적어 두었다.
    assert await error_code(client, workspace, spec("https://notexample.com/")) == (
        "http_host_not_allowed"
    )


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.0.8",
        "192.168.1.1",
        "172.16.0.1",
        "0.0.0.0",  # noqa: S104 (바인드가 아니라 차단 대상 주소다)
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1%eth0",
        "::ffff:127.0.0.1",
        "2002:7f00:1::1",
    ],
)
async def test_names_resolving_to_private_addresses_are_blocked(
    tmp_path: Path, workspace: Path, address: str
) -> None:
    calls: list[httpx.Request] = []
    client = make_client(
        tmp_path,
        lambda r: calls.append(r) or httpx.Response(200),
        dns={"example.com": [PUBLIC, address]},
    )

    assert await error_code(client, workspace, spec("https://example.com/")) == (
        "http_address_blocked"
    )
    assert calls == []
    await client.aclose()


async def test_dns_failure(tmp_path: Path, workspace: Path) -> None:
    client = make_client(tmp_path, lambda r: httpx.Response(200), dns={})

    assert await error_code(client, workspace, spec("https://example.com/")) == "http_dns_failed"
    await client.aclose()


async def test_ip_literals_need_their_own_entry(tmp_path: Path, workspace: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    star = make_client(tmp_path, handler, entries=["*"])
    assert await error_code(star, workspace, spec("http://8.8.8.8/")) == "http_host_not_allowed"
    await star.aclose()

    listed = make_client(tmp_path, handler, entries=["8.8.8.8", "127.0.0.1"])
    result = await listed.request(spec("http://8.8.8.8/"), workspace_root=workspace)
    assert result.status == 204
    assert seen[0].url.host == "8.8.8.8"
    assert "sni_hostname" not in seen[0].extensions
    # 목록에 있어도 루프백은 막힌다(A11(e)).
    assert await error_code(listed, workspace, spec("http://127.0.0.1:8500/healthz")) == (
        "http_address_blocked"
    )
    await listed.aclose()


async def test_connection_is_pinned_to_the_checked_address(
    client: HttpClient, workspace: Path, seen: list[httpx.Request]
) -> None:
    result = await client.request(
        spec(
            "https://example.com:8443/path?q=1",
            headers={
                "X-Custom": "1",
                "Host": "evil.test",
                "Connection": "upgrade-me",
                "Content-Length": "999",
            },
        ),
        workspace_root=workspace,
    )

    request = seen[0]
    assert request.url.host == PUBLIC
    assert request.url.port == 8443
    assert request.url.raw_path == b"/path?q=1"
    assert request.headers["host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["x-custom"] == "1"
    # 모델이 준 연결·길이 헤더는 버리고 클라이언트 기본값을 쓴다.
    assert request.headers.get("connection") != "upgrade-me"
    assert request.headers.get("content-length") != "999"
    assert result.url == "https://example.com:8443/path?q=1"
    assert result.body == "안녕"
    assert result.saved_path is None and not result.truncated


async def test_model_supplied_user_agent_is_kept(
    client: HttpClient, workspace: Path, seen: list[httpx.Request]
) -> None:
    await client.request(
        spec("https://example.com/", headers={"user-agent": "custom/1"}), workspace_root=workspace
    )

    assert seen[0].headers["user-agent"] == "custom/1"


async def test_every_redirect_hop_is_checked_again(tmp_path: Path, workspace: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://cdn.example.com/next"})
        return httpx.Response(200, text="done", headers={"content-type": "text/plain"})

    private_hop = make_client(
        tmp_path, handler, dns={"example.com": [PUBLIC], "cdn.example.com": ["10.1.2.3"]}
    )
    assert await error_code(private_hop, workspace, spec("https://example.com/")) == (
        "http_address_blocked"
    )
    await private_hop.aclose()

    other_host = make_client(tmp_path, handler, entries=["example.com"])
    assert await error_code(other_host, workspace, spec("https://example.com/")) == (
        "http_host_not_allowed"
    )
    await other_host.aclose()

    allowed = make_client(tmp_path, handler)
    result = await allowed.request(spec("https://example.com/"), workspace_root=workspace)
    assert (result.status, result.body) == (200, "done")
    assert result.redirects == ["http://cdn.example.com/next"]
    assert result.url == "http://cdn.example.com/next"
    await allowed.aclose()


async def test_redirect_limit(tmp_path: Path, workspace: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.strip("/") or 0)
        return httpx.Response(302, headers={"location": f"/{step + 1}"})

    client = make_client(tmp_path, handler)
    assert await error_code(client, workspace, spec("https://example.com/")) == (
        "http_redirect_limit"
    )
    await client.aclose()
    assert MAX_REDIRECTS == 3


async def test_redirect_after_post_becomes_get_and_drops_credentials(
    tmp_path: Path, workspace: Path
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(303, headers={"location": "https://cdn.example.com/result"})
        return httpx.Response(200, json={"ok": True})

    client = make_client(tmp_path, handler)
    result = await client.request(
        spec(
            "https://example.com/submit",
            method="POST",
            body='{"a": 1}',
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        ),
        workspace_root=workspace,
    )
    await client.aclose()

    assert seen[0].method == "POST" and seen[0].content == b'{"a": 1}'
    assert seen[1].method == "GET" and seen[1].content == b""
    assert "authorization" not in seen[1].headers
    assert "content-type" not in seen[1].headers
    assert result.body == '{"ok":true}'


async def test_large_text_is_truncated_and_saved(tmp_path: Path, workspace: Path) -> None:
    body = ("가나다라" * 10_000).encode()
    client = make_client(
        tmp_path,
        lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"}),
    )

    result = await client.request(spec("https://example.com/big"), workspace_root=workspace)
    await client.aclose()

    assert result.truncated
    assert result.body_bytes == len(body)
    assert result.saved_path == "downloads/call_1.json"
    assert (workspace / "downloads" / "call_1.json").read_bytes() == body
    assert result.body is not None
    assert len(result.body.encode()) <= TEXT_LIMIT + 3


async def test_binary_bodies_are_always_saved(tmp_path: Path, workspace: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(100)
    client = make_client(
        tmp_path, lambda r: httpx.Response(200, content=png, headers={"content-type": "image/png"})
    )

    result = await client.request(
        spec("https://example.com/a.png", exec_id="call:2/x"), workspace_root=workspace
    )
    await client.aclose()

    assert result.body is None
    assert result.saved_path == "downloads/call_2_x.png"
    assert (workspace / "downloads" / "call_2_x.png").read_bytes() == png


async def test_oversized_bodies_are_refused_and_not_kept(tmp_path: Path, workspace: Path) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 300

    client = make_client(
        tmp_path,
        lambda r: httpx.Response(200, content=chunks(), headers={"content-type": "image/png"}),
        max_body=1000,
    )

    assert await error_code(client, workspace, spec("https://example.com/")) == (
        "http_response_too_large"
    )
    await client.aclose()
    # 받다 만 파일을 지웠다.
    assert (workspace / "downloads").is_dir()
    assert list((workspace / "downloads").iterdir()) == []


async def test_head_requests_have_no_body(
    client: HttpClient, workspace: Path, seen: list[httpx.Request]
) -> None:
    result = await client.request(
        spec("https://example.com/", method="HEAD"), workspace_root=workspace
    )

    assert seen[0].method == "HEAD"
    assert result.body == "" and result.body_bytes == 0


async def test_request_body_limit(client: HttpClient, workspace: Path) -> None:
    big = "x" * (REQUEST_BODY_LIMIT + 1)

    assert await error_code(client, workspace, spec("https://example.com/", "POST", body=big)) == (
        "http_request_too_large"
    )


async def test_header_injection_is_refused(client: HttpClient, workspace: Path) -> None:
    request = spec("https://example.com/", headers={"X-A": "1\r\nX-Injected: 2"})

    assert await error_code(client, workspace, request) == "http_header_invalid"


async def test_timeouts_become_sandbox_errors(tmp_path: Path, workspace: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(tmp_path, handler)
    assert await error_code(client, workspace, spec("https://example.com/")) == "http_timeout"
    await client.aclose()


async def test_declared_charset_is_used(tmp_path: Path, workspace: Path) -> None:
    client = make_client(
        tmp_path,
        lambda r: httpx.Response(
            200,
            content="한글".encode("euc-kr"),
            headers={"content-type": "text/html; charset=EUC-KR"},
        ),
    )

    result = await client.request(spec("https://example.com/"), workspace_root=workspace)
    await client.aclose()

    assert result.body == "한글"


def test_response_headers_are_capped_and_sensitive_ones_masked() -> None:
    masked = mask_headers(
        {"Authorization": "Bearer x", "cookie": "a=1", "X-Api-Key": "k", "Accept": "*/*"}
    )

    assert masked == {"Authorization": "***", "cookie": "***", "X-Api-Key": "***", "Accept": "*/*"}


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        (PUBLIC, True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.1", False),
        ("100.64.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("fd00::1", False),
        ("255.255.255.255", False),
    ],
)
def test_public_address_rules(address: str, public: bool) -> None:
    assert is_public_address(address) is public


@pytest.mark.parametrize(
    ("content_type", "textual"),
    [
        ("text/html; charset=utf-8", True),
        ("application/json", True),
        ("application/problem+json", True),
        ("application/atom+xml", True),
        ("application/xml", True),
        ("application/octet-stream", False),
        ("image/png", False),
        (None, False),
    ],
)
def test_textual_content_types(content_type: str | None, textual: bool) -> None:
    assert is_textual(content_type) is textual
