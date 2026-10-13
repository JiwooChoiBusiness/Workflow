import asyncio

import structlog
from fastapi import APIRouter, Request, Response

from app.api.errors import ApiError
from app.auth.dependencies import (
    SESSION_COOKIE,
    CurrentAuth,
    DbDep,
    clear_session_cookie,
    set_session_cookie,
)
from app.auth.session_store import SessionStore
from app.db.models import Account, Workspace
from app.db.repositories.accounts import AccountRepository
from app.db.repositories.login_sessions import LoginSessionRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.schemas.auth import AccountOut, LoginRequest, MeOut, PasswordChangeRequest
from app.schemas.workspace import WorkspaceSummary
from app.security.passwords import dummy_hash, hash_password, needs_rehash, verify_password
from app.security.rate_limit import SlidingWindowLimiter

log = structlog.get_logger("app.api.console.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_ATTEMPTS_PER_MINUTE = 5


def new_login_limiter() -> SlidingWindowLimiter:
    return SlidingWindowLimiter(LOGIN_ATTEMPTS_PER_MINUTE, 60.0)


def _check_rate(request: Request, key: str) -> None:
    limiter: SlidingWindowLimiter = request.app.state.login_limiter
    decision = limiter.hit(key)
    if not decision.allowed:
        raise ApiError(
            429,
            "rate_limited",
            "시도가 너무 많다. 잠시 뒤 다시 시도한다",
            headers={"Retry-After": str(decision.retry_after_sec)},
        )


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _me(account: Account, workspace: Workspace) -> MeOut:
    return MeOut(
        account=AccountOut.model_validate(account),
        workspace=WorkspaceSummary(id=workspace.id, name=workspace.name, slug=workspace.slug),
    )


def _password_matches(account: Account | None, password: str) -> bool:
    # 계정이 없어도 같은 시간 동안 검증해 응답 시간으로 계정 유무가 드러나지 않게 한다.
    stored = account.password_hash if account is not None else dummy_hash()
    return verify_password(stored, password) and account is not None


def _invalid_credentials() -> ApiError:
    return ApiError(401, "invalid_credentials", "이메일 또는 비밀번호가 맞지 않다")


@router.post("/login", response_model=MeOut)
async def login(body: LoginRequest, request: Request, response: Response, db: DbDep) -> MeOut:
    # argon2 검증이 무거워 성공·실패와 관계없이 모든 시도를 센다.
    _check_rate(request, f"login:{_client_host(request)}")
    async with db.read() as session:
        account = await AccountRepository(session).get_by_email(body.email)
        workspace = await WorkspaceRepository(session).get_default()
    password = body.password.get_secret_value()
    matched = await asyncio.to_thread(_password_matches, account, password)
    if account is None or workspace is None or not matched:
        log.info("auth.login_failed")
        raise _invalid_credentials()

    if needs_rehash(account.password_hash):
        rehashed = await asyncio.to_thread(hash_password, password)
        async with db.write() as session:
            await AccountRepository(session).set_password(account.id, rehashed)

    issued = await SessionStore(db).issue(account.id, request.headers.get("user-agent"))
    account.last_login_at = issued.issued_at
    set_session_cookie(response, issued.token)
    log.info("auth.login", account_id=str(account.id))
    return _me(account, workspace)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: DbDep) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await SessionStore(db).revoke(token)
    clear_session_cookie(response)


@router.get("/me", response_model=MeOut)
async def me(auth: CurrentAuth, db: DbDep) -> MeOut:
    async with db.read() as session:
        workspace = await WorkspaceRepository(session).get(auth.workspace_id)
    if workspace is None:
        raise ApiError(401, "unauthorized", "로그인이 필요하다")
    return _me(auth.account, workspace)


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordChangeRequest, request: Request, auth: CurrentAuth, db: DbDep
) -> None:
    _check_rate(request, f"password:{auth.account.id}")
    current = body.current_password.get_secret_value()
    if not await asyncio.to_thread(verify_password, auth.account.password_hash, current):
        raise ApiError(400, "wrong_password", "현재 비밀번호가 맞지 않다")
    new_hash = await asyncio.to_thread(hash_password, body.new_password.get_secret_value())
    async with db.write() as session:
        await AccountRepository(session).set_password(auth.account.id, new_hash)
        # 지금 쓰는 세션만 남기고 다른 곳의 로그인은 모두 끊는다(설계 6.2).
        revoked = await LoginSessionRepository(session).delete_for_account(
            auth.account.id, keep=auth.token_hash
        )
    log.info("auth.password_changed", account_id=str(auth.account.id), revoked_sessions=revoked)
