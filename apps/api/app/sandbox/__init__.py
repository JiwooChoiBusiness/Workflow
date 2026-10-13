from app.config import Settings
from app.sandbox.base import SandboxBackend

E2B_NOT_IMPLEMENTED = "E2B backend is not implemented yet"


def make_backend(settings: Settings) -> SandboxBackend:
    """SANDBOX_BACKEND에 맞는 백엔드. 백엔드 선택은 여기 한 곳이다(3.15).

    e2b는 아직 구현이 없어 기동이 실패해야 하므로 만들지 않고 바로 오류를 낸다.
    """
    if settings.sandbox_backend == "e2b":
        raise NotImplementedError(E2B_NOT_IMPLEMENTED)
    from app.sandbox.local_workspace import LocalWorkspaceBackend

    return LocalWorkspaceBackend(settings)
