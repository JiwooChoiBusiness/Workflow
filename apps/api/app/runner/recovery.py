from app.db.engine import Database
from app.runner.supervisor import Supervisor


async def recover(db: Database, supervisor: Supervisor) -> None:
    # 런·작업공간 테이블이 생기는 M6에서 채운다. running → failed(code=shutdown),
    # queued → 큐에 재투입, workspace_instance busy → ready.
    return None
