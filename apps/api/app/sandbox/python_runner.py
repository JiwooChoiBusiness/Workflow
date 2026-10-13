import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from app.sandbox.base import SandboxError

BOOTSTRAP_NAME = "_bootstrap.py"
REQUIREMENTS_PATH = ".platform/requirements.txt"
PIP_TIMEOUT_SEC = 600

# -I가 PYTHONPATH를 무시하므로 부트스트랩이 .packages를 sys.path 끝에 붙인다. 끝에 붙여야 작업공간의
# json.py 같은 파일이 표준 라이브러리를 가리지 못한다. 작업공간 위치는 __file__에서 구한다.
# 백업을 다른 DATA_DIR로 옮겨도 그대로 동작해야 하기 때문이다. addsitedir는 .pth도 처리한다.
BOOTSTRAP_SOURCE = """\
# 플랫폼이 관리하는 파일이다. run_python 코드를 작업공간 기준으로 실행한다.
import os
import runpy
import site
import sys

_workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
site.addsitedir(os.path.join(_workspace, ".packages"))
_target = sys.argv[1]
sys.argv = sys.argv[1:]
os.chdir(os.environ.get("SANDBOX_WORKSPACE_CWD") or _workspace)
runpy.run_path(_target, run_name="__main__")
"""

# 이름, extras, 버전 조건만 받는다. URL·로컬 경로·옵션(-e, --index-url)은 거부(3.8).
PIP_SPEC_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_.,-]+\])?\s*"
    r"([<>=!~]=?\s*[A-Za-z0-9.*+!-]+(\s*,\s*[<>=!~]=?\s*[A-Za-z0-9.*+!-]+)*)?$"
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def validate_pip_specs(specs: Sequence[str]) -> list[str]:
    cleaned = [spec.strip() for spec in specs]
    if not cleaned:
        raise SandboxError("pip_spec_invalid", "설치할 패키지가 없다")
    for spec in cleaned:
        if not PIP_SPEC_PATTERN.fullmatch(spec):
            raise SandboxError(
                "pip_spec_invalid",
                f"패키지 이름과 버전 조건만 쓸 수 있다(URL·경로·옵션 불가): {spec[:80]!r}",
            )
    return cleaned


def normalized_name(spec: str) -> str:
    match = _NAME.match(spec.strip())
    if match is None:
        raise SandboxError("pip_spec_invalid", f"패키지 이름을 찾지 못했다: {spec[:80]!r}")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def merge_requirements(existing: Iterable[str], new_specs: Iterable[str]) -> list[str]:
    """설치 이력에 새 스펙을 더한다. 같은 이름(PEP 503 정규화)은 새 스펙으로 바꾼다."""
    merged: dict[str, str] = {}
    for line in existing:
        line = line.strip()
        if not line or line.startswith("#") or not PIP_SPEC_PATTERN.fullmatch(line):
            continue
        merged[normalized_name(line)] = line
    for spec in new_specs:
        merged[normalized_name(spec)] = spec.strip()
    return list(merged.values())


def parse_requirements(text: str) -> list[str]:
    return merge_requirements(text.splitlines(), [])


def run_python_argv(python: Path, bootstrap: Path, script: Path) -> list[str]:
    # -I는 PYTHON* 환경변수를 무시하므로 출력 인코딩은 -X utf8로 고정한다.
    return [str(python), "-I", "-X", "utf8", str(bootstrap), str(script)]


def pip_install_argv(python: Path, target: Path, specs: Sequence[str]) -> list[str]:
    return [
        str(python),
        "-I",
        "-X",
        "utf8",
        "-m",
        "pip",
        "install",
        "--target",
        str(target),
        "--upgrade",
        "--no-input",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        *specs,
    ]
