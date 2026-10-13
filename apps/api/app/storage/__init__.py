from app.config import Settings
from app.storage.base import StorageBackend


def make_storage(settings: Settings) -> StorageBackend:
    from app.storage.local_fs import LocalFsStorage

    return LocalFsStorage(settings.data_dir / "storage")
