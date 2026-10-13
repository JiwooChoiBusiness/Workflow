from pathlib import Path

import pytest

from app.sandbox.base import SandboxError
from app.sandbox.python_runner import (
    BOOTSTRAP_SOURCE,
    merge_requirements,
    normalized_name,
    parse_requirements,
    pip_install_argv,
    run_python_argv,
    validate_pip_specs,
)


@pytest.mark.parametrize(
    "spec",
    [
        "requests",
        "requests==2.*",
        "requests>=2,<3",
        "requests >= 2.31, != 2.32.0",
        "numpy~=1.26",
        "pandas[excel]>=2",
        "uvicorn[standard,watch]",
        "scikit-learn",
        "zope.interface",
        "typing_extensions!=4.0",
        "  requests  ",
    ],
)
def test_allowed_specs(spec: str) -> None:
    assert validate_pip_specs([spec]) == [spec.strip()]


@pytest.mark.parametrize(
    "spec",
    [
        "../x",
        "./pkg",
        "git+https://github.com/x/y",
        "https://example.com/x.whl",
        "--index-url",
        "--index-url=https://evil.example/simple",
        "-e .",
        "-r requirements.txt",
        r"C:\x.whl",
        "/opt/wheels/x.whl",
        "requests ; python_version<'3'",
        "requests @ https://example.com/r.whl",
        "requests==2.0 --hash=sha256:abc",
        "",
        "requests[socks",
        "pkg; rm -rf /",
        "pkg|x",
        "pkg\nnext",
        "pkg==1.0 extra",
    ],
)
def test_rejected_specs(spec: str) -> None:
    with pytest.raises(SandboxError) as info:
        validate_pip_specs(["requests", spec])
    assert info.value.code == "pip_spec_invalid"


def test_empty_package_list_is_rejected() -> None:
    with pytest.raises(SandboxError):
        validate_pip_specs([])


def test_names_are_normalized_like_pep503() -> None:
    assert normalized_name("Foo_Bar.baz>=1") == "foo-bar-baz"
    assert normalized_name("requests[socks]==2") == "requests"


def test_install_history_keeps_one_spec_per_name() -> None:
    merged = merge_requirements(
        ["requests==2.0", "# 주석", "Foo_Bar>=1", "numpy", "--index-url x"],
        ["requests>=2.31", "foo-bar==2", "pandas"],
    )

    assert merged == ["requests>=2.31", "foo-bar==2", "numpy", "pandas"]


def test_parse_requirements_skips_invalid_lines() -> None:
    assert parse_requirements("requests\n--index-url x\n\nnumpy==1\n-e .\n") == [
        "requests",
        "numpy==1",
    ]


def test_pip_argv(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    target = tmp_path / "ws" / ".packages"

    argv = pip_install_argv(python, target, ["requests", "numpy==1.26"])

    assert argv[:6] == [str(python), "-I", "-X", "utf8", "-m", "pip"]
    assert argv[6] == "install"
    assert argv[argv.index("--target") + 1] == str(target)
    for flag in (
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
    ):
        assert flag in argv
    assert argv[-2:] == ["requests", "numpy==1.26"]


def test_run_python_argv(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    bootstrap = tmp_path / ".run" / "_bootstrap.py"
    script = tmp_path / ".run" / "call_1.py"

    assert run_python_argv(python, bootstrap, script) == [
        str(python),
        "-I",
        "-X",
        "utf8",
        str(bootstrap),
        str(script),
    ]


def test_bootstrap_appends_packages_after_the_standard_library() -> None:
    compile(BOOTSTRAP_SOURCE, "_bootstrap.py", "exec")
    assert "site.addsitedir" in BOOTSTRAP_SOURCE
    assert "sys.path.insert" not in BOOTSTRAP_SOURCE
    assert 'run_name="__main__"' in BOOTSTRAP_SOURCE
