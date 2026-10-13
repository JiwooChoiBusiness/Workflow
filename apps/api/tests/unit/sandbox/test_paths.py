import sys
from pathlib import Path
from typing import Any

import pytest

from app.sandbox.base import SandboxError
from app.sandbox.paths import (
    MAX_INPUT_LENGTH,
    MAX_RESOLVED_LENGTH,
    relative_posix,
    resolve_cwd,
    resolve_in_workspace,
    split_relative,
)

BOTH = ["nt", "posix"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace.resolve()


def error_code(root: Path, user_path: str, **kwargs: Any) -> str:
    with pytest.raises(SandboxError) as info:
        resolve_in_workspace(root, user_path, **kwargs)
    return info.value.code


def make_dir_link(link: Path, target: Path) -> None:
    """심볼릭 링크를 만들고, 권한이 없는 Windows에서는 정션으로 대신한다."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


@pytest.mark.parametrize("os_name", BOTH)
@pytest.mark.parametrize(
    "user_path",
    ["a.txt", "dir/sub/file.csv", "a/../b.txt", ".", "outputs/", "한글 파일.txt", ".hidden/x"],
)
def test_relative_paths_stay_inside(root: Path, user_path: str, os_name: str) -> None:
    resolved = resolve_in_workspace(root, user_path, os_name=os_name)

    assert resolved.is_relative_to(root)


def test_backslash_is_a_separator_only_with_windows_rules(root: Path) -> None:
    assert (
        resolve_in_workspace(root, r"dir\sub\f.txt", os_name="nt") == root / "dir" / "sub" / "f.txt"
    )
    with pytest.raises(SandboxError):
        split_relative(r"dir\f.txt", os_name="posix")


@pytest.mark.parametrize("os_name", BOTH)
@pytest.mark.parametrize(
    "user_path",
    [
        "",
        "x" * (MAX_INPUT_LENGTH + 1),
        "a\0b",
        "a\nb",
        "a\x1fb",
        "a<b",
        "a>b",
        'a"b',
        "a|b",
        "a?b",
        "a*b",
        "file.txt:stream",
        "C:x",
    ],
)
def test_invalid_strings(root: Path, user_path: str, os_name: str) -> None:
    assert error_code(root, user_path, os_name=os_name) == "path_invalid"


@pytest.mark.parametrize("os_name", BOTH)
@pytest.mark.parametrize(
    "user_path",
    [
        r"\\?\C:\Windows\win.ini",
        r"\\.\pipe\x",
        r"\\server\share\x",
        "//server/share/x",
        r"C:\Windows\win.ini",
        "C:/Windows/win.ini",
        r"\Windows\win.ini",
        "/etc/passwd",
    ],
)
def test_absolute_unc_and_device_paths(root: Path, user_path: str, os_name: str) -> None:
    assert error_code(root, user_path, os_name=os_name) == "path_invalid"


@pytest.mark.parametrize("user_path", [r"..\..\app.db", "../../app.db", "a/../../x", ".."])
def test_parent_escape_with_windows_rules(root: Path, user_path: str) -> None:
    assert error_code(root, user_path, os_name="nt") == "path_outside_workspace"


@pytest.mark.parametrize("user_path", ["../../app.db", "a/../../x", ".."])
def test_parent_escape_with_posix_rules(root: Path, user_path: str) -> None:
    assert error_code(root, user_path, os_name="posix") == "path_outside_workspace"


@pytest.mark.parametrize("os_name", BOTH)
@pytest.mark.parametrize(
    "user_path",
    ["CON", "con.txt", "nul", "Nul.tar.gz", "COM1.log", "lpt9", "aux", "PRN.md", "dir/CON/x"],
)
def test_reserved_names(root: Path, user_path: str, os_name: str) -> None:
    assert error_code(root, user_path, os_name=os_name) == "path_invalid"


@pytest.mark.parametrize("user_path", ["CONsole.txt", "com10", "lpt", "nul_file", "a.con", "a.nul"])
def test_names_that_only_look_reserved(root: Path, user_path: str) -> None:
    assert resolve_in_workspace(root, user_path, os_name="nt") == root / user_path


@pytest.mark.parametrize("os_name", BOTH)
@pytest.mark.parametrize("user_path", ["a.", "b ", "dir./x", "dir /x", "..."])
def test_trailing_dot_or_space(root: Path, user_path: str, os_name: str) -> None:
    assert error_code(root, user_path, os_name=os_name) == "path_invalid"


def test_resolved_path_length_limit(root: Path) -> None:
    budget = MAX_RESOLVED_LENGTH - len(str(root)) - 1
    if budget + 1 > MAX_INPUT_LENGTH:
        pytest.skip("임시 폴더 경로가 짧아 200자 입력으로 250자를 넘길 수 없다")
    fits = "/".join(["d" * 9] * (budget // 10) + ["f" * (budget % 10 or 1)])
    fits = fits[:budget]

    assert len(str(resolve_in_workspace(root, fits))) <= MAX_RESOLVED_LENGTH
    assert error_code(root, fits + "x") == "path_too_long"


@pytest.mark.parametrize(
    "user_path", [".platform/x", ".run/a.ps1", ".PLATFORM/x", "x/../.run/y", ".platform", ".Run"]
)
def test_platform_area_is_read_only(root: Path, user_path: str) -> None:
    assert error_code(root, user_path, for_write=True) == "path_forbidden"
    resolve_in_workspace(root, user_path)


@pytest.mark.parametrize(
    "user_path", [".platformx/y", "sub/.platform/x", "sub/.run/x", ".packages/m.py"]
)
def test_other_dot_folders_are_writable(root: Path, user_path: str) -> None:
    assert resolve_in_workspace(root, user_path, for_write=True).is_relative_to(root)


def test_link_pointing_outside_is_rejected(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    make_dir_link(root / "link", outside)

    assert error_code(root, "link/secret.txt") == "path_outside_workspace"
    assert error_code(root, "link/new.txt", for_write=True) == "path_outside_workspace"
    assert error_code(root, "link") == "path_outside_workspace"


def test_dangling_link_pointing_outside_is_rejected(root: Path, tmp_path: Path) -> None:
    target = tmp_path / "later"
    target.mkdir()
    make_dir_link(root / "dangling", target)
    target.rmdir()

    assert error_code(root, "dangling/x.txt") == "path_outside_workspace"


def test_link_inside_workspace_is_followed(root: Path) -> None:
    (root / "real").mkdir()
    make_dir_link(root / "alias", root / "real")

    assert resolve_in_workspace(root, "alias/f.txt") == root / "real" / "f.txt"


@pytest.mark.skipif(sys.platform != "win32", reason="정션은 Windows에만 있다")
def test_junction_to_windows_directory(root: Path) -> None:
    assert sys.platform == "win32"
    import _winapi

    windir = Path("C:/Windows")
    _winapi.CreateJunction(str(windir), str(root / "j"))
    try:
        assert error_code(root, "j/win.ini") == "path_outside_workspace"
        assert error_code(root, r"j\System32\drivers\etc\hosts") == "path_outside_workspace"
    finally:
        # 정션만 지운다. rmdir은 링크 자체를 없애고 대상은 건드리지 않는다.
        (root / "j").rmdir()


def test_resolve_cwd(root: Path) -> None:
    (root / "sub").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")

    assert resolve_cwd(root, ".") == root
    assert resolve_cwd(root, "sub") == root / "sub"
    with pytest.raises(SandboxError) as missing:
        resolve_cwd(root, "missing")
    assert missing.value.code == "not_found"
    with pytest.raises(SandboxError) as not_dir:
        resolve_cwd(root, "file.txt")
    assert not_dir.value.code == "not_a_directory"


def test_relative_posix(root: Path) -> None:
    assert relative_posix(root, root) == "."
    assert relative_posix(root, root / "a" / "b.txt") == "a/b.txt"
