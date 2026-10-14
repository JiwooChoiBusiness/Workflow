import argparse
import getpass
import runpy
import sys
from pathlib import Path

# 설정·로그·asyncio는 명령 안에서 불러온다. serve --reload의 감시 프로세스는 이것들 없이 자식을
# 띄우기만 해서 20MB 안팎으로 돈다(설계 M10.1 (3)).


def _serve(reload: bool) -> int:
    if reload:
        from app.runner import reloader

        # 감시 프로세스가 띄운 자식은 같은 명령으로 불리지만 환경변수를 보고 서버를 직접 띄운다.
        if not reloader.is_child():
            return reloader.run([sys.executable, "-m", "app.cli", "serve", "--reload"])

    import uvicorn

    from app.config import get_settings
    from app.logsetup import configure_logging

    settings = get_settings()
    # create_app이 파일 로그까지 붙이기 전에 uvicorn이 내는 로그도 같은 JSON 형식으로 나오게 한다.
    configure_logging(settings.log_level, secrets=settings.secret_values())
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=settings.server_port,
        lifespan="on",
        log_config=None,
    )
    return 0


def serve(reload: bool = False) -> int:
    # main.py를 직접 실행했을 때도 `platform serve`와 같은 경로를 타게 한다.
    return _serve(reload)


def _doctor() -> int:
    from app import sysinfo
    from app.config import get_settings

    checks = sysinfo.doctor_checks(get_settings())
    for check in checks:
        print(f"{'OK  ' if check.ok else 'WARN'}  {check.name}: {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _backup(out: str | None, with_workspaces: bool, keep: int | None) -> int:
    import sqlite3

    from app import backup
    from app.config import get_settings
    from app.sandbox.disk import format_size

    settings = get_settings()
    warning = backup.server_warning(settings)
    if warning is not None:
        print(f"주의: {warning}", file=sys.stderr)
    if out is not None and keep is not None:
        print("주의: --out을 주면 --keep으로 정리하지 않는다", file=sys.stderr)
    try:
        result = backup.create_backup(
            settings,
            out=None if out is None else Path(out),
            with_workspaces=with_workspaces,
            keep=backup.DEFAULT_KEEP if keep is None else keep,
        )
    except backup.BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        # 쓰다 만 <폴더>.part는 create_backup이 이미 지웠다.
        print(f"backup: 실패했다. {exc}", file=sys.stderr)
        return 1

    manifest = result.manifest
    print(f"backup: {result.path} (파일 {manifest['files']}개, {format_size(manifest['bytes'])})")
    print(
        f"backup: 스키마 {manifest['alembic_revision'] or '없음'},"
        f" 암호화 키 fingerprint {manifest['encryption_key_fingerprint'] or '없음'}"
    )
    if result.links_skipped:
        print(f"backup: 링크·정션 {result.links_skipped}개는 따라가지 않았다")
    if result.vanished:
        print(f"backup: 복사하는 사이에 사라진 파일·폴더 {result.vanished}개는 빠졌다")
    if result.pruned:
        names = ", ".join(path.name for path in result.pruned)
        print(f"backup: 오래된 백업 {len(result.pruned)}개를 지웠다: {names}")
    for path in result.prune_failed:
        print(f"backup: 오래된 백업 {path}를 지우지 못했다", file=sys.stderr)
    if not result.healthy:
        print(
            f"backup: 복사본의 quick_check가 실패했다: {manifest['quick_check']}."
            " 원본 DB를 점검한다. 예전 백업은 정리하지 않았다",
            file=sys.stderr,
        )
        return 1
    return 0


def _memcheck(args: list[str]) -> int:
    from app.config import REPO_ROOT

    # `python scripts/memcheck.py`가 서버 의존성 없이도 돌아야 해서 측정 코드는 스크립트에 둔다.
    script = REPO_ROOT / "scripts" / "memcheck.py"
    return runpy.run_path(str(script))["main"](args)


def _migrate() -> int:
    import asyncio

    from app.config import get_settings
    from app.db.engine import Database
    from app.db.migrate import current_revision, upgrade_head
    from app.logsetup import configure_logging

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
    import asyncio

    from sqlalchemy.exc import OperationalError

    from app.config import get_settings
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


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"1 이상의 정수여야 한다: {value!r}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform", description="Workflow 플랫폼 서버 명령")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="127.0.0.1:<SERVER_PORT>에 서버를 띄운다")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="apps/api/app·migrations의 .py가 바뀌면 서버를 다시 띄운다. 감시 프로세스가 하나 더"
        " 떠서 개발 중에만 쓴다",
    )
    commands.add_parser(
        "doctor", help="셸·파이썬·pip·데이터 경로·실행 정책·포트·암호화 키를 점검한다"
    )
    backup = commands.add_parser(
        "backup",
        help="DB·storage·허용 목록을 DATA_DIR과 같은 구조로 복사한다. 서버가 떠 있어도 된다",
    )
    backup.add_argument(
        "--out",
        metavar="폴더",
        help="결과 폴더. 아직 없는 경로여야 한다. 주지 않으면 backups/<YYYYMMDD-HHMMSS>",
    )
    backup.add_argument(
        "--with-workspaces", action="store_true", help="세션 작업공간(workspaces/)도 복사한다"
    )
    backup.add_argument(
        "--keep",
        type=_positive_int,
        metavar="N",
        help="backups/에 남길 백업 수(기본 5). --out을 주면 정리하지 않는다",
    )
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

    args, extra = parser.parse_known_args(argv)
    if args.command == "memcheck":
        return _memcheck(extra)
    if extra:
        parser.error(f"알 수 없는 인자: {' '.join(extra)}")
    if args.command == "serve":
        return _serve(reload=args.reload)
    if args.command == "doctor":
        return _doctor()
    if args.command == "backup":
        return _backup(args.out, args.with_workspaces, args.keep)
    if args.command == "migrate":
        return _migrate()
    return _reset_password(from_stdin=args.password_stdin)


if __name__ == "__main__":
    sys.exit(main())
