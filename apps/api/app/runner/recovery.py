"""기동 복구(설계 2.6의 4번).

running인 런은 실행하던 프로세스가 사라진 것이므로 failed(code=shutdown)로 닫고
run.error·run.completed를 남긴다. queued는 아직 아무것도 실행하지 않았으므로 큐에 다시 넣는다.
waiting_input은 답을 기다리는 그대로 둔다. 런이 쓰던 작업공간은 busy에서 ready로 돌린다. 도구
프로세스는 Job Object의 KILL_ON_JOB_CLOSE로 이미 함께 죽었다.
"""

import structlog

from app.db.engine import Database
from app.db.models.enums import RunStatus, WorkspaceInstanceStatus
from app.db.repositories.runs import RunRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import utcnow
from app.runner.supervisor import Supervisor
from app.schemas.events import UsageTotal
from app.streaming.event_bus import EventBus, RunRef

log = structlog.get_logger("app.runner")

SHUTDOWN_MESSAGE = "서버가 런 도중 종료되었다. 다시 보내면 이어서 작업한다"


def _run_key(ref: RunRef) -> str:
    # app.services.runs.run_key와 같은 형식. 복구가 서비스 계층에 기대지 않게 여기서 만든다.
    return f"{ref.workspace_id}:{ref.run_id}"


async def recover(db: Database, supervisor: Supervisor, bus: EventBus) -> dict[str, int]:
    now = utcnow()
    failed: list[tuple[RunRef, dict[str, object], dict[str, object]]] = []
    requeue: list[RunRef] = []
    released = 0
    async with db.read() as session:
        workspace_ids = [row.id for row in await WorkspaceRepository(session).list_all()]
    for workspace_id in workspace_ids:
        async with db.write() as session:
            runs = RunRepository(session, workspace_id)
            for run in await runs.list_by_status([RunStatus.RUNNING]):
                error: dict[str, object] = {
                    "code": "shutdown",
                    "message": SHUTDOWN_MESSAGE,
                    "retryable": True,
                }
                run.status = RunStatus.FAILED
                run.error = error
                run.pending_interaction = None
                run.ended_at = now
                ref = RunRef(run_id=run.id, session_id=run.session_id, workspace_id=workspace_id)
                failed.append(
                    (
                        ref,
                        error,
                        {
                            "status": RunStatus.FAILED,
                            "usage_total": UsageTotal.model_validate(run.usage or {}).model_dump(),
                            "steps": run.steps,
                            "error": error,
                        },
                    )
                )
            for run in await runs.list_by_status([RunStatus.QUEUED]):
                requeue.append(
                    RunRef(run_id=run.id, session_id=run.session_id, workspace_id=workspace_id)
                )
            instances = WorkspaceInstanceRepository(session, workspace_id)
            for instance in await instances.list_by_status(WorkspaceInstanceStatus.BUSY):
                instance.status = WorkspaceInstanceStatus.READY
                released += 1
    for ref, error, completed in failed:
        await bus.publish(ref, "run.error", error)
        await bus.publish(ref, "run.completed", completed)
        bus.end(ref.run_id)
    for position, ref in enumerate(requeue, start=1):
        # 채널을 미리 열어 두어 재기동 직후 붙는 화면이 실행을 따라갈 수 있게 한다.
        await bus.publish(ref, "run.queued", {"position": position})
        supervisor.enqueue(_run_key(ref))
    result = {"failed": len(failed), "requeued": len(requeue), "released": released}
    if any(result.values()):
        log.warning("runner.recovered", **result)
    return result
