import os
from pathlib import Path

import pytest

from app.sandbox.allowlist import (
    DEFAULT_ENTRIES,
    Allowlist,
    AllowlistRules,
    ensure_default_file,
    normalize_host,
)


def test_exact_and_suffix_entries() -> None:
    rules = AllowlistRules.parse(
        [
            "# 주석",
            "",
            "PyPI.org",
            ".pythonhosted.org   # 하위 도메인",
            "github.com.",
        ]
    )

    assert rules.allows("pypi.org")
    assert rules.allows("PYPI.ORG.")
    assert not rules.allows("www.pypi.org")
    assert rules.allows("files.pythonhosted.org")
    assert rules.allows("a.b.pythonhosted.org")
    # .suffix는 그 도메인 자신을 포함하지 않는다(3.9).
    assert not rules.allows("pythonhosted.org")
    assert not rules.allows("evilpythonhosted.org")
    assert rules.allows("github.com")
    assert not rules.allows("example.com")
    assert not rules.allows("")


def test_star_allows_names_but_not_ip_literals() -> None:
    star = AllowlistRules.parse(["*"])

    assert star.allows("example.com")
    assert not star.allows("8.8.8.8")
    assert not star.allows("2001:4860:4860::8888")
    assert AllowlistRules.parse(["*", "8.8.8.8"]).allows("8.8.8.8")


def test_ip_literals_compare_in_canonical_form() -> None:
    rules = AllowlistRules.parse(["203.0.113.7", "[2001:DB8::1]"])

    assert rules.allows("203.0.113.7")
    assert not rules.allows("203.0.113.8")
    assert rules.allows("2001:db8::1")
    assert rules.allows("2001:0db8:0:0:0:0:0:1")
    assert rules.allows("[2001:db8::1]")


def test_internationalized_names() -> None:
    rules = AllowlistRules.parse(["bücher.de"])

    assert normalize_host("BÜCHER.de") == "xn--bcher-kva.de"
    assert rules.allows("xn--bcher-kva.de")
    assert rules.allows("Bücher.DE")


def test_default_file_is_created_once(tmp_path: Path) -> None:
    path = tmp_path / "data" / "allowlist.txt"

    assert ensure_default_file(path)
    text = path.read_text(encoding="utf-8")
    for entry in DEFAULT_ENTRIES:
        assert f"\n{entry}\n" in text
    path.write_text("example.com\n", encoding="utf-8")
    assert not ensure_default_file(path)
    assert path.read_text(encoding="utf-8") == "example.com\n"


def test_default_entries_cover_package_and_source_hosts(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.txt"
    ensure_default_file(path)
    allowlist = Allowlist(path)

    for host in (
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
    ):
        assert allowlist.allows(host), host
    assert not allowlist.allows("example.com")
    assert not allowlist.allows("8.8.8.8")


def test_file_changes_are_picked_up_after_the_check_interval(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.txt"
    path.write_text("a.example\n", encoding="utf-8")
    now = [100.0]
    allowlist = Allowlist(path, check_interval=2.0, clock=lambda: now[0])

    assert allowlist.allows("a.example")
    path.write_text("b.example\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    now[0] += 1.0
    assert allowlist.allows("a.example")
    now[0] += 1.0
    assert allowlist.allows("b.example")
    assert not allowlist.allows("a.example")


def test_missing_file_allows_nothing(tmp_path: Path) -> None:
    allowlist = Allowlist(tmp_path / "missing.txt")

    assert not allowlist.allows("pypi.org")


@pytest.mark.parametrize("entry", ["pypi.org", ".pypi.org", "*"])
def test_bom_written_by_windows_editors_is_ignored(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "allowlist.txt"
    path.write_bytes(f"{entry}\r\n".encode("utf-8-sig"))

    assert Allowlist(path).allows("files.pypi.org" if entry != "pypi.org" else "pypi.org")
