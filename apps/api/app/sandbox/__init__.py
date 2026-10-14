from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings
from app.sandbox.base import SandboxBackend

if TYPE_CHECKING:
    from app.sandbox.http_client import HttpClient

E2B_NOT_IMPLEMENTED = "E2B backend is not implemented yet"


def make_backend(
    settings: Settings,
    *,
    http_client: "HttpClient | None" = None,
    workspaces_dir: Path | None = None,
) -> SandboxBackend:
    """SANDBOX_BACKEND에 맞는 백엔드. 백엔드 선택은 여기 한 곳이다(3.15).

    e2b는 아직 구현이 없어 기동이 실패해야 하므로 만들지 않고 바로 오류를 낸다. http_client를
    주면 허용 목록 객체를 그 클라이언트와 나눠 쓴다. workspaces_dir은 두 번째 인스턴스(워크플로우
    작업 폴더)를 띄울 때 준다.
    """
    if settings.sandbox_backend == "e2b":
        raise NotImplementedError(E2B_NOT_IMPLEMENTED)
    from app.sandbox.local_workspace import LocalWorkspaceBackend

    return LocalWorkspaceBackend(settings, http_client=http_client, workspaces_dir=workspaces_dir)
