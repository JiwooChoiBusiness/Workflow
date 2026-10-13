import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from app.sandbox.base import SandboxError

MAX_INPUT_LENGTH = 200
# LongPathsEnabled가 꺼진 Windows에서 OSError가 나기 전에 명확한 오류를 준다(3.5의 4번).
MAX_RESOLVED_LENGTH = 250
# 쓰기 도구로 건드릴 수 없는 플랫폼 관리 영역.
PROTECTED_TOP_DIRS = frozenset({".platform", ".run"})

# ':'는 드라이브 문자와 NTFS 대체 스트림(file.txt:stream)을 함께 막는다.
_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def is_reserved_name(name: str) -> bool:
    """CON, nul.txt처럼 확장자 앞부분이 Windows 장치 이름인지."""
    return name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES


def _invalid(user_path: str, reason: str) -> SandboxError:
    return SandboxError("path_invalid", f"{reason}: {user_path[:80]!r}")


def split_relative(user_path: str, *, os_name: str = os.name) -> tuple[str, ...]:
    """3.5의 문자열 규칙(1·2·5번)을 검사하고 경로 구성 요소를 돌려준다.

    os_name은 구분자와 절대 경로 판정 방식만 바꾼다. 금지 문자·예약 이름 규칙은 어느 OS에서나
    적용한다. 스냅샷과 스킬 패키지가 Windows에서 그대로 풀려야 하기 때문이다.
    """
    if not user_path:
        raise _invalid(user_path, "빈 경로")
    if len(user_path) > MAX_INPUT_LENGTH:
        raise _invalid(user_path, f"경로가 {MAX_INPUT_LENGTH}자를 넘는다")
    if any(ord(char) < 0x20 or char == "\x7f" for char in user_path):
        raise _invalid(user_path, "제어 문자가 들어 있다")
    forbidden = _FORBIDDEN_CHARS.intersection(user_path)
    if forbidden:
        raise _invalid(user_path, f"쓸 수 없는 문자 {''.join(sorted(forbidden))}")
    unified = user_path.replace("\\", "/")
    # \\?\, \\.\, \\server\share, //server/share
    if unified.startswith("//"):
        raise _invalid(user_path, "UNC·장치 경로는 쓸 수 없다")
    pure: PurePath
    if os_name == "nt":
        pure = PureWindowsPath(user_path)
    else:
        if "\\" in user_path:
            raise _invalid(user_path, "구분자는 '/'만 쓴다")
        pure = PurePosixPath(user_path)
    if pure.is_absolute() or pure.drive or pure.root or unified.startswith("/"):
        raise _invalid(user_path, "작업공간 상대 경로만 쓸 수 있다")
    for part in pure.parts:
        if part == "..":
            continue
        if part.endswith((".", " ")):
            # NTFS가 끝의 점·공백을 조용히 잘라 다른 파일에 쓴다.
            raise _invalid(user_path, f"구성 요소 {part!r}가 점이나 공백으로 끝난다")
        if is_reserved_name(part):
            raise _invalid(user_path, f"Windows 예약 이름 {part!r}")
    return pure.parts


def resolve_in_workspace(
    root: Path, user_path: str, *, for_write: bool = False, os_name: str = os.name
) -> Path:
    """작업공간 상대 경로를 검사해 실제 절대 경로로 바꾼다(3.5).

    resolve가 심볼릭 링크와 정션을 따라가므로 작업공간 안의 링크가 밖을 가리키면 여기서 걸린다.
    파일 입출력은 반드시 돌려준 경로로 한다. 검사한 경로와 쓰는 경로가 같아야 한다.
    """
    parts = split_relative(user_path, os_name=os_name)
    try:
        root_resolved = root.resolve()
        resolved = root_resolved.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid(user_path, f"경로를 해석하지 못했다({exc})") from exc
    if not resolved.is_relative_to(root_resolved):
        raise SandboxError("path_outside_workspace", f"작업공간 밖을 가리킨다: {user_path[:80]!r}")
    if len(str(resolved)) > MAX_RESOLVED_LENGTH:
        raise SandboxError(
            "path_too_long",
            f"절대 경로가 {MAX_RESOLVED_LENGTH}자를 넘는다({len(str(resolved))}자). "
            "더 짧은 경로를 쓴다",
        )
    if for_write:
        relative = resolved.relative_to(root_resolved).parts
        # Windows는 대소문자를 가리지 않으므로 .PLATFORM도 같은 폴더다.
        if relative and relative[0].casefold() in PROTECTED_TOP_DIRS:
            raise SandboxError(
                "path_forbidden", f"플랫폼 관리 영역에는 쓸 수 없다: {user_path[:80]!r}"
            )
    return resolved


def resolve_cwd(root: Path, cwd: str, *, os_name: str = os.name) -> Path:
    resolved = resolve_in_workspace(root, cwd, os_name=os_name)
    if not resolved.exists():
        raise SandboxError("not_found", f"작업 디렉터리가 없다: {cwd[:80]!r}", status=404)
    if not resolved.is_dir():
        raise SandboxError("not_a_directory", f"디렉터리가 아니다: {cwd[:80]!r}")
    return resolved


def relative_posix(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return "." if relative in ("", ".") else relative
