import asyncio

import structlog
from fastapi import APIRouter, HTTPException

from app.auth.dependencies import DbDep, SettingsDep
from app.config import Settings
from app.db.engine import Database
from app.db.repositories.accounts import AccountRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.schemas.auth import AccountOut, MeOut, SetupRequest, SetupStatus
from app.schemas.workspace import WorkspaceSettings, WorkspaceSummary
from app.security.passwords import MIN_PASSWORD_LENGTH, hash_password

log = structlog.get_logger("app.api.console.setup")

router = APIRouter(prefix="/setup", tags=["setup"])

DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_WORKSPACE_NAME = "기본 워크스페이스"


def initial_workspace_settings(settings: Settings) -> WorkspaceSettings:
    return WorkspaceSettings(
        approval_mode_default=settings.approval_mode_default,
        run_event_retention_days=settings.run_event_retention_days,
    )


async def _ensure_not_set_up(db: Database) -> None:
    # 계정이 생긴 뒤에는 이 경로가 없는 것처럼 404를 준다(설계 6.2, A2).
    async with db.read() as session:
        if await AccountRepository(session).count() > 0:
            raise HTTPException(status_code=404)


@router.get("", response_model=SetupStatus)
async def setup_status(db: DbDep) -> SetupStatus:
    await _ensure_not_set_up(db)
    return SetupStatus(needs_setup=True, password_min_length=MIN_PASSWORD_LENGTH)


@router.post("", status_code=201, response_model=MeOut)
async def run_setup(body: SetupRequest, db: DbDep, settings: SettingsDep) -> MeOut:
    await _ensure_not_set_up(db)
    # argon2는 수십 ms 걸리므로 쓰기 잠금을 잡기 전에 스레드에서 계산한다.
    password_hash = await asyncio.to_thread(hash_password, body.password.get_secret_value())
    async with db.write() as session:
        accounts = AccountRepository(session)
        # 위 확인과 이 트랜잭션 사이에 다른 요청이 먼저 계정을 만들었을 수 있다.
        if await accounts.count() > 0:
            raise HTTPException(status_code=404)
        workspaces = WorkspaceRepository(session)
        workspace = await workspaces.get_default()
        if workspace is None:
            workspace = workspaces.add(
                name=body.workspace_name or DEFAULT_WORKSPACE_NAME,
                slug=DEFAULT_WORKSPACE_SLUG,
                settings=initial_workspace_settings(settings).model_dump(mode="json"),
            )
        account = accounts.add(
            email=body.email,
            name=body.name or body.email.split("@", 1)[0],
            password_hash=password_hash,
        )
        await session.flush()
    log.info("setup.completed", account_id=str(account.id), workspace_id=str(workspace.id))
    return MeOut(
        account=AccountOut.model_validate(account),
        workspace=WorkspaceSummary(id=workspace.id, name=workspace.name, slug=workspace.slug),
    )
