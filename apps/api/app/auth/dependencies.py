import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response

from app.api.errors import ApiError
from app.auth.session_store import SESSION_TTL, SessionStore
from app.config import Settings
from app.db.engine import Database
from app.db.models import Account
from app.db.repositories.workspaces import WorkspaceRepository
from app.security.rate_limit import SlidingWindowLimiter
from app.services.api_keys import ApiKeyService, AuthenticatedApiKey

SESSION_COOKIE = "sid"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


DbDep = Annotated[Database, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def allowed_origins(settings: Settings) -> frozenset[str]:
    port = settings.server_port
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", *settings.dev_origins}
    return frozenset(origin.rstrip("/").lower() for origin in origins)


async def csrf_protect(request: Request, settings: SettingsDep) -> None:
    # 브라우저는 다른 출처에서 JSON 본문이나 X-Requested-With를 보내려면 사전 요청(preflight)을
    # 거쳐야 하고, 서버는 CORS를 허용하지 않으므로 이 두 헤더 조건만으로 위조 요청이 막힌다.
    # Origin은 브라우저가 보낼 때만 검사한다. curl 같은 비브라우저 클라이언트는 Origin이 없다.
    if request.method not in _UNSAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/").lower() not in allowed_origins(settings):
        raise ApiError(403, "origin_not_allowed", "허용되지 않은 출처(Origin)의 요청이다")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    requested_with = request.headers.get("x-requested-with", "").lower()
    if media_type != "application/json" and requested_with != "fetch":
        # 멀티파트 업로드는 Content-Type을 바꿀 수 없어 X-Requested-With: fetch로 대신한다.
        raise ApiError(
            415,
            "json_required",
            "변경 요청은 Content-Type: application/json 또는 X-Requested-With: fetch가 필요하다",
        )


def set_session_cookie(response: Response, token: str) -> None:
    # 127.0.0.1의 평문 HTTP라 Secure는 붙일 수 없다.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


@dataclass(frozen=True)
class AuthContext:
    account: Account
    workspace_id: uuid.UUID
    token_hash: str


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "로그인이 필요하다")


async def current_auth(request: Request, response: Response, db: DbDep) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _unauthorized()
    active = await SessionStore(db).resolve(token)
    if active is None:
        raise _unauthorized()
    async with db.read() as session:
        # 1단계는 계정 1개·워크스페이스 1개라 계정과 워크스페이스를 잇는 열이 없다.
        workspace = await WorkspaceRepository(session).get_default()
    if workspace is None:
        raise _unauthorized()
    if active.refreshed:
        set_session_cookie(response, token)
    return AuthContext(
        account=active.account, workspace_id=workspace.id, token_hash=active.token_hash
    )


CurrentAuth = Annotated[AuthContext, Depends(current_auth)]


def _bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


async def api_key_auth(request: Request, db: DbDep) -> AuthenticatedApiKey:
    """서비스 API의 Bearer 인증(6.1)과 키당 레이트 리밋(분당 60번, 6.4).

    쿠키는 보지 않는다. 키에 묶인 에이전트 범위는 각 경로가 SessionScope로 검사한다.
    """
    challenge = {"WWW-Authenticate": "Bearer"}
    raw = _bearer_token(request)
    if raw is None:
        raise ApiError(
            401, "unauthorized", "Authorization: Bearer <API 키> 헤더가 필요하다", headers=challenge
        )
    key = await ApiKeyService(db).authenticate(raw)
    if key is None:
        raise ApiError(401, "invalid_api_key", "API 키가 없거나 폐기·만료되었다", headers=challenge)
    limiter: SlidingWindowLimiter = request.app.state.api_limiter
    decision = limiter.hit(str(key.key_id))
    if not decision.allowed:
        raise ApiError(
            429,
            "rate_limited",
            f"API 키 하나로 분당 {limiter.limit}번까지 부를 수 있다. 잠시 뒤 다시 부른다",
            headers={"Retry-After": str(decision.retry_after_sec)},
        )
    return key


ApiKeyAuth = Annotated[AuthenticatedApiKey, Depends(api_key_auth)]


def require_scope(key: AuthenticatedApiKey, scope: str) -> None:
    if scope not in key.scopes:
        raise ApiError(403, "scope_not_allowed", f"이 API 키에는 {scope} 권한이 없다")
