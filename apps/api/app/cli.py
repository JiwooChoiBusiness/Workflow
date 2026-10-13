import argparse
import asyncio
import getpass
import runpy
import sys
from pathlib import Path

from app.config import REPO_ROOT, get_settings
from app.logging import configure_logging

_NOT_YET = {"backup": "M10"}


def _serve(reload: bool) -> int:
    import uvicorn

    from app.runner import winconsole

    settings = get_settings()
    # 리로드 감시 프로세스의 로그만 콘솔로 낸다. 앱 프로세스는 create_app에서 파일 로그까지 붙인다.
    configure_logging(settings.log_level, secrets=settings.secret_values())
    warning = winconsole.reload_warning() if reload else None
    if warning is not None:
        print(f"주의: {warning}", file=sys.stderr)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=settings.server_port,
        reload=reload,
        reload_dirs=[str(Path(__file__).parent)] if reload else None,
        lifespan="on",
        log_config=None,
    )
    return 0


def _doctor() -> int:
    from app import sysinfo

    checks = sysinfo.doctor_checks(get_settings())
    for check in checks:
        print(f"{'OK  ' if check.ok else 'WARN'}  {check.name}: {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _memcheck(args: list[str]) -> int:
    # `python scripts/memcheck.py`가 서버 의존성 없이도 돌아야 해서 측정 코드는 스크립트에 둔다.
    script = REPO_ROOT / "scripts" / "memcheck.py"
    return runpy.run_path(str(script))["main"](args)


def _migrate() -> int:
    from app.db.engine import Database
    from app.db.migrate import current_revision, upgrade_head

    settings = get_settings()
    configure_logging(settings.log_level, secrets=settings.secret_values())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "app.db"

    async def run() -> str | None:
        db = Database(path)
        try:
            await upgrade_head(db)
            return await current_revision(db)
        finally:
            await db.close()

    revision = asyncio.run(run())
    print(f"migrate: {path} 스키마 리비전 {revision}")
    return 0


def _read_new_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("새 비밀번호: ")
    if getpass.getpass("한 번 더: ") != first:
        print("reset-password: 두 번 입력한 비밀번호가 다르다.", file=sys.stderr)
        return None
    return first


def _reset_password(from_stdin: bool) -> int:
    from sqlalchemy.exc import OperationalError

    from app.db.engine import Database
    from app.db.repositories.accounts import AccountRepository
    from app.db.repositories.login_sessions import LoginSessionRepository
    from app.security.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password

    path = get_settings().data_dir / "app.db"
    # 없는 파일을 열면 빈 DB가 새로 생기므로 먼저 확인한다.
    if not path.is_file():
        print(
            f"reset-password: {path}가 없다. 서버를 한 번 띄워 초기 설정을 한다.", file=sys.stderr
        )
        return 1
    password = _read_new_password(from_stdin)
    if password is None:
        return 1
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        print(
            f"reset-password: 비밀번호는 {MIN_PASSWORD_LENGTH}~{MAX_PASSWORD_LENGTH}자여야 한다.",
            file=sys.stderr,
        )
        return 1
    password_hash = hash_password(password)

    async def run() -> tuple[str, int] | None:
        db = Database(path)
        try:
            async with db.write() as session:
                accounts = AccountRepository(session)
                account = await accounts.first()
                if account is None:
                    return None
                await accounts.set_password(account.id, password_hash)
                # 비밀번호를 잃어버렸다면 기존 로그인도 믿을 수 없으니 모두 끊는다.
                revoked = await LoginSessionRepository(session).delete_for_account(account.id)
                return account.email, revoked
        finally:
            await db.close()

    try:
        result = asyncio.run(run())
    except OperationalError:
        print(
            "reset-password: 스키마가 없다. `uv run platform migrate`를 먼저 실행한다.",
            file=sys.stderr,
        )
        return 1
    if result is None:
        print("reset-password: 계정이 없다. 화면에서 초기 설정을 먼저 한다.", file=sys.stderr)
        return 1
    email, revoked = result
    print(f"reset-password: {email}의 비밀번호를 바꾸고 로그인 세션 {revoked}개를 끝냈다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform", description="Workflow 플랫폼 서버 명령")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="127.0.0.1:<SERVER_PORT>에 서버를 띄운다")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="코드가 바뀌면 다시 띄운다. 감시 프로세스가 하나 더 떠서 개발 중에만 쓴다."
        " Windows에서는 셸·파이썬 도구가 실행되지 않는다",
    )
    commands.add_parser("doctor", help="셸·파이썬·pip·데이터 경로·실행 정책·포트를 점검한다")
    # 인자(--help 포함)는 scripts/memcheck.py가 그대로 받는다.
    commands.add_parser(
        "memcheck",
        help="서버를 따로 띄워 1초마다 RSS를 재고 최댓값을 출력한다",
        add_help=False,
    )
    commands.add_parser(
        "migrate", help="DATA_DIR의 SQLite를 최신 스키마로 올린다. 서버 기동 때도 자동으로 한다"
    )
    reset = commands.add_parser(
        "reset-password", help="로컬 계정의 비밀번호를 바꾸고 로그인 세션을 모두 끊는다"
    )
    reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="묻지 않고 표준 입력의 첫 줄을 새 비밀번호로 쓴다(스크립트용)",
    )
    for name, milestone in _NOT_YET.items():
        commands.add_parser(name, help=f"{milestone}에서 구현한다")

    args, extra = parser.parse_known_args(argv)
    if args.command == "memcheck":
        return _memcheck(extra)
    if extra:
        parser.error(f"알 수 없는 인자: {' '.join(extra)}")
    if args.command == "serve":
        return _serve(reload=args.reload)
    if args.command == "doctor":
        return _doctor()
    if args.command == "migrate":
        return _migrate()
    if args.command == "reset-password":
        return _reset_password(from_stdin=args.password_stdin)
    print(
        f"platform {args.command}: 아직 없다. {_NOT_YET[args.command]}에서 구현한다.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
