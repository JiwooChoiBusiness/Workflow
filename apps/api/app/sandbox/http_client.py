import asyncio
import codecs
import ipaddress
import mimetypes
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import BinaryIO

import httpx

from app.sandbox.allowlist import Allowlist, normalize_host
from app.sandbox.base import HttpResult, HttpSpec, SandboxError

TEXT_LIMIT = 64 * 1024
REQUEST_BODY_LIMIT = 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SEC = 60
MAX_TIMEOUT_SEC = 300
MAX_RESPONSE_HEADERS = 50
USER_AGENT = "WorkflowElon-Agent/1.0"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 모델이 주더라도 보내지 않는 헤더. 연결과 본문 길이는 클라이언트가 정한다.
DROPPED_REQUEST_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
# 다른 호스트로 리다이렉트될 때 떼어 내는 자격 증명 헤더.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "x-api-key", "proxy-authorization"})
# run_event·승인 카드에 기록할 때 값을 가리는 헤더(3.9의 8번, S8).
SENSITIVE_HEADERS = CREDENTIAL_HEADERS | {"set-cookie"}
MASK = "***"

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
_TEXT_TYPES = frozenset({"application/json", "application/xml"})
_SAFE_STEM = re.compile(r"[^A-Za-z0-9_.-]")

Resolver = Callable[[str, int], Awaitable[list[str]]]


def mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: MASK if name.lower() in SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


def is_public_address(address: str) -> bool:
    # 링크 로컬 IPv6의 %scope는 떼고 본다.
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 매핑·6to4 주소는 안쪽 IPv4로 판정한다.
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None and not is_public_address(str(ip.sixtofour)):
            return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return not any(ip in network for network in _BLOCKED_NETWORKS)


def is_textual(content_type: str | None) -> bool:
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return (
        media.startswith("text/")
        or media in _TEXT_TYPES
        or media.endswith("+json")
        or media.endswith("+xml")
    )


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _clamp_timeout(value: int) -> int:
    return max(1, min(value, MAX_TIMEOUT_SEC))


def _request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        if any(char in name or char in value for char in "\r\n\0") or not name.strip():
            raise SandboxError("http_header_invalid", f"헤더가 올바르지 않다: {name[:40]!r}")
        if name.strip().lower() in DROPPED_REQUEST_HEADERS:
            continue
        result[name.strip()] = value
    if not any(name.lower() == "user-agent" for name in result):
        result["User-Agent"] = USER_AGENT
    return result


def _parse_url(raw: str) -> httpx.URL:
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise SandboxError("http_url_invalid", f"URL을 해석하지 못했다: {raw[:200]!r}") from exc
    if url.scheme not in ("http", "https"):
        raise SandboxError("http_url_invalid", "http와 https만 쓸 수 있다")
    if url.userinfo:
        raise SandboxError("http_url_invalid", "URL에 사용자 정보(user:pass@)를 넣을 수 없다")
    if not url.host:
        raise SandboxError("http_url_invalid", "URL에 호스트가 없다")
    return url


def _extension(content_type: str | None, textual: bool) -> str:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(media) if media else None
    if guessed:
        return guessed
    return ".txt" if textual else ".bin"


def _charset(response: httpx.Response) -> str:
    charset = response.charset_encoding
    if charset:
        try:
            codecs.lookup(charset)
        except LookupError:
            return "utf-8"
        return charset
    return "utf-8"


class HttpClient:
    """http_request 도구의 실제 구현. 승인 없이 나가는 유일한 네트워크 경로라 여기서 통제한다."""

    def __init__(
        self,
        allowlist: Allowlist,
        *,
        max_body_bytes: int,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowlist = allowlist
        self._max_body = max_body_bytes
        self._resolve = resolver or system_resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            # 연결을 재사용하지 않는다. 호스트를 IP로 바꿔 접속하므로 풀이 IP 기준으로 묶는다.
            # 같은 IP를 쓰는 다른 호스트가 앞 호스트의 TLS 연결을 넘겨받지 않게 한다.
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
            verify=True,
            follow_redirects=False,
            # 환경변수 프록시·.netrc를 따르지 않는다. 접속 IP를 직접 고정해야 하기 때문이다.
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_target(self, url: httpx.URL) -> tuple[str, str]:
        """허용 목록 → DNS → 공개 주소 검사(3.9의 3번). (호스트, 접속할 IP)를 돌려준다."""
        host = normalize_host(url.host)
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise SandboxError("http_address_blocked", f"로컬 호스트에는 접속할 수 없다: {host}")
        if not self._allowlist.allows(host):
            raise SandboxError(
                "http_host_not_allowed",
                f"허용 목록에 없는 호스트다: {host}. {self._allowlist.path}에 추가할 수 있다",
            )
        if _is_ip_literal(host):
            if not is_public_address(host):
                raise SandboxError("http_address_blocked", f"공개 주소가 아니다: {host}")
            return host, host
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            raise SandboxError("http_dns_failed", f"{host}를 해석하지 못했다: {exc}") from exc
        if not addresses:
            raise SandboxError("http_dns_failed", f"{host}의 주소가 없다")
        for address in addresses:
            if not is_public_address(address):
                raise SandboxError(
                    "http_address_blocked", f"{host}가 공개 주소가 아닌 {address}로 해석된다"
                )
        return host, addresses[0]

    async def request(self, spec: HttpSpec, *, workspace_root: Path) -> HttpResult:
        timeout = _clamp_timeout(spec.timeout_sec)
        body = spec.body.encode("utf-8") if spec.body is not None else None
        if body is not None and len(body) > REQUEST_BODY_LIMIT:
            raise SandboxError(
                "http_request_too_large", f"요청 본문은 {REQUEST_BODY_LIMIT}바이트까지다"
            )
        try:
            async with asyncio.timeout(timeout):
                return await self._request(spec, body, timeout, workspace_root)
        except TimeoutError as exc:
            raise SandboxError("http_timeout", f"{timeout}초 안에 끝나지 않았다") from exc

    async def _request(
        self, spec: HttpSpec, body: bytes | None, timeout: int, workspace_root: Path
    ) -> HttpResult:
        method: str = spec.method
        url = _parse_url(spec.url)
        headers = _request_headers(spec.headers)
        redirects: list[str] = []
        while True:
            host, address = await self.check_target(url)
            response = await self._send(method, url, host, address, headers, body, timeout)
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUSES or not location:
                try:
                    return await self._read(spec, method, url, response, redirects, workspace_root)
                finally:
                    await response.aclose()
            await response.aclose()
            if len(redirects) >= MAX_REDIRECTS:
                raise SandboxError(
                    "http_redirect_limit", f"리다이렉트가 {MAX_REDIRECTS}번을 넘는다"
                )
            next_url = _parse_url(str(url.join(location)))
            redirects.append(str(next_url))
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method not in ("GET", "HEAD")
            ):
                method, body = "GET", None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            if normalize_host(next_url.host) != normalize_host(url.host):
                headers = {k: v for k, v in headers.items() if k.lower() not in CREDENTIAL_HEADERS}
            url = next_url

    async def _send(
        self,
        method: str,
        url: httpx.URL,
        host: str,
        address: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: int,
    ) -> httpx.Response:
        # 검사한 IP로 접속을 고정해 DNS 리바인딩을 막는다. Host 헤더와 SNI는 원래 호스트다.
        pinned = url.copy_with(host=address)
        extensions: dict[str, str] = {}
        if not _is_ip_literal(host):
            extensions["sni_hostname"] = url.raw_host.decode("ascii")
        request = self._client.build_request(
            method,
            pinned,
            headers={**headers, "Host": url.netloc.decode("ascii")},
            content=body,
            extensions=extensions,
            timeout=httpx.Timeout(timeout),
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise SandboxError("http_timeout", f"{host}: 시간 초과") from exc
        except httpx.HTTPError as exc:
            raise SandboxError("http_failed", f"{host}: {exc}") from exc

    async def _read(
        self,
        spec: HttpSpec,
        method: str,
        url: httpx.URL,
        response: httpx.Response,
        redirects: list[str],
        workspace_root: Path,
    ) -> HttpResult:
        content_type = response.headers.get("content-type")
        textual = is_textual(content_type)
        head = bytearray()
        total = 0
        file: BinaryIO | None = None
        target: Path | None = None
        try:
            try:
                if method != "HEAD":
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_body:
                            raise SandboxError(
                                "http_response_too_large",
                                f"응답 본문이 HTTP_MAX_BODY_BYTES({self._max_body})를 넘어 "
                                "중단했다",
                            )
                        if file is None and (not textual or total > TEXT_LIMIT):
                            target = self._download_path(
                                spec, content_type, textual, workspace_root
                            )
                            file = target.open("wb")
                            file.write(head)
                        if file is not None:
                            file.write(chunk)
                        if textual and len(head) < TEXT_LIMIT:
                            head.extend(chunk[: TEXT_LIMIT - len(head)])
            except httpx.TimeoutException as exc:
                raise SandboxError("http_timeout", f"{url.host}: 본문을 받다가 시간 초과") from exc
            except httpx.HTTPError as exc:
                raise SandboxError("http_failed", f"{url.host}: {exc}") from exc
        except BaseException:
            # 받다 만 파일은 남기지 않는다.
            if file is not None:
                file.close()
                file = None
            if target is not None:
                target.unlink(missing_ok=True)
            raise
        finally:
            if file is not None:
                file.close()

        headers: dict[str, str] = {}
        for name, value in response.headers.multi_items():
            if name in headers:
                headers[name] = f"{headers[name]}, {value}"
            elif len(headers) < MAX_RESPONSE_HEADERS:
                headers[name] = value
        text = bytes(head).decode(_charset(response), errors="replace") if textual else None
        return HttpResult(
            status=response.status_code,
            url=str(url),
            headers=headers,
            content_type=content_type,
            body=text,
            body_bytes=total,
            truncated=textual and total > TEXT_LIMIT,
            saved_path=None if target is None else target.relative_to(workspace_root).as_posix(),
            redirects=redirects,
        )

    def _download_path(
        self, spec: HttpSpec, content_type: str | None, textual: bool, workspace_root: Path
    ) -> Path:
        downloads = workspace_root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        stem = _SAFE_STEM.sub("_", spec.exec_id)[:64] or "response"
        return downloads / f"{stem}{_extension(content_type, textual)}"
