"""기동 복구(1단계 설계 2.6의 4번, 2단계 설계 3.7의 5번).

워크플로우 런을 먼저 본다(recover_workflows). running인 워크플로우 런은 실행하던 프로세스가 사라진
것이므로 failed(code=shutdown)로 닫는다. 체크포인트가 있어도 자동으로 잇지 않는다(노드의 부수
효과를 두 번 내지 않으려고). 끝난 워크플로우 런이 기다리던 자식 런(agent 노드의 과업 런) 중 queued·
waiting_input인 것은 cancelled(code=orphaned)로 닫고, 그 자식이 지휘자였으면 손자도 같은
함수(child_runs.cancel_orphans)로 닫는다. 이 일을 1단계 복구보다 먼저 해야 1단계 복구가 아무도
기다리지 않는 queued 자식을 다시 넣지 않는다.

그다음 1단계 복구(recover)다. running인 런은 failed(code=shutdown)로 닫고 run.error·run.completed를
남긴다. queued는 아직 아무것도 실행하지 않았으므로 큐에 다시 넣는다. waiting_input은 답을 기다리는
그대로 둔다. 런이 쓰던 작업공간은 busy에서 ready로 돌린다. 도구 프로세스는 Job Object의
KILL_ON_JOB_CLOSE로 이미 함께 죽었다.

하위 에이전트 호출(오케스트레이션 3.8)은 1단계 순서 사이에 두 단계를 끼운다. 실패로 닫은 런이
부모였으면 그 queued·waiting_input 자식(손자까지)을 cancelled(orphaned)로 닫고, 자식이었으면 그
자식을 기다리던 부모를 claim_parent로 queued + resume_payload로 돌린다. 그 부모는 아래 queued
재투입에 함께 들어가고, 투입 전에 child_run.completed를 먼저 낸다. 모두 워크스페이스별 쓰기 트랜잭션
안에서 상태만 쓰고 발행·투입은 트랜잭션 밖에서 한다. 놓친 것은 기동 끝의 reconcile_children이
잡는다.
"""

from typing import Any

import structlog

from app.db.engine import Database
from app.db.models import Run
from app.db.models.enums import RunStatus, WorkspaceInstanceStatus
from app.db.repositories.runs import RunRepository
from app.db.repositories.workflows import WfNodeRunRepository, WfRunRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import utcnow
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.schemas.events import UsageTotal
from app.services.child_runs import (
    ClaimedParent,
    cancel_orphans,
    child_completed_data,
    claim_parent,
    orphan_runs,
)
from app.services.workflow_runs import completed_data as wf_completed_data
from app.services.workflow_runs import pending_of, wf_run_ref
from app.streaming.event_bus import EventBus, RunRef

log = structlog.get_logger("app.runner")

SHUTDOWN_MESSAGE = "서버가 런 도중 종료되었다. 다시 보내면 이어서 작업한다"
WF_SHUTDOWN_MESSAGE = "서버가 런 도중 종료되었다. 다시 실행한다"
ORPHANED_MESSAGE = "이 런을 기다리던 워크플로우 런이 끝나 함께 취소했다"
ENDED_WF_STATUSES = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


def _run_key(ref: RunRef) -> str:
    # app.services.runs.run_key와 같은 형식. 복구가 서비스 계층에 기대지 않게 여기서 만든다.
    return f"{ref.workspace_id}:{ref.run_id}"


async def recover_workflows(db: Database, supervisor: Supervisor, bus: EventBus) -> dict[str, int]:
    now = utcnow()
    failed: list[tuple[RunRef, dict[str, Any], dict[str, Any]]] = []
    orphaned: list[tuple[RunRef, dict[str, object]]] = []
    requeue: list[RunRef] = []
    resumed = 0
    async with db.read() as session:
        workspace_ids = [row.id for row in await WorkspaceRepository(session).list_all()]
    for workspace_id in workspace_ids:
        async with db.write() as session:
            runs = WfRunRepository(session, workspace_id)
            nodes = WfNodeRunRepository(session, workspace_id)
            for run in await runs.list_by_status([RunStatus.RUNNING]):
                error: dict[str, Any] = {
                    "code": "shutdown",
                    "message": WF_SHUTDOWN_MESSAGE,
                    "retryable": True,
                }
                run.status = RunStatus.FAILED
                run.error = error
                run.pending_interaction = None
                run.resume_payload = None
                run.ended_at = now
                await nodes.close_running(run.id, now)
                failed.append(
                    (wf_run_ref(run), error, wf_completed_data(run, status=RunStatus.FAILED))
                )
            await session.flush()
            children_runs = await nodes.orphaned_children(
                parent_statuses=ENDED_WF_STATUSES,
                child_statuses=(RunStatus.QUEUED, RunStatus.WAITING_INPUT),
            )
            orphaned.extend(
                await orphan_runs(
                    session, workspace_id, children_runs, now, message=ORPHANED_MESSAGE
                )
            )
            # 자식이 지휘자(call_agent)였으면 그 아래 손자도 기다릴 부모가 없다.
            orphaned.extend(
                await cancel_orphans(session, workspace_id, [run.id for run in children_runs], now)
            )
            children = RunRepository(session, workspace_id)
            for run in await runs.list_by_status([RunStatus.WAITING_INPUT]):
                pending = pending_of(run)
                if pending is None or pending.child_run_id is None:
                    continue
                child = await children.get(pending.child_run_id)
                if child is not None and child.status == RunStatus.WAITING_INPUT:
                    continue
                # 자식은 이미 답을 받았는데(또는 끝났는데) 부모를 다시 넣기 전에 죽었다.
                run.status = RunStatus.QUEUED
                run.resume_payload = {"node_id": pending.node_id}
                resumed += 1
            for run in await runs.list_by_status([RunStatus.QUEUED]):
                requeue.append(wf_run_ref(run))
    for ref, error, completed in failed:
        await bus.publish(ref, "workflow.error", error)
        await bus.publish(ref, "workflow.completed", completed)
        bus.end(ref.run_id)
    for ref, completed in orphaned:
        await bus.publish(ref, "run.completed", completed)
        bus.end(ref.run_id)
    for position, ref in enumerate(requeue, start=1):
        # 채널을 미리 열어 두어 재기동 직후 붙는 화면이 실행을 따라갈 수 있게 한다.
        await bus.publish(ref, "workflow.queued", {"position": position})
        try:
            supervisor.enqueue(_run_key(ref))
        except SupervisorClosed:
            break
    result = {
        "failed": len(failed),
        "orphaned": len(orphaned),
        "resumed": resumed,
        "requeued": len(requeue),
    }
    if any(result.values()):
        log.warning("workflow.recovered", **result)
    return result


async def recover(db: Database, supervisor: Supervisor, bus: EventBus) -> dict[str, int]:
    now = utcnow()
    failed: list[tuple[RunRef, dict[str, object], dict[str, object]]] = []
    orphaned: list[tuple[RunRef, dict[str, object]]] = []
    resumed: list[ClaimedParent] = []
    requeue: list[RunRef] = []
    released = 0
    async with db.read() as session:
        workspace_ids = [row.id for row in await WorkspaceRepository(session).list_all()]
    for workspace_id in workspace_ids:
        async with db.write() as session:
            runs = RunRepository(session, workspace_id)
            failed_runs: list[Run] = []
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
                failed_runs.append(run)
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
            await session.flush()
            # 실패한 런이 부모였으면 그 자식·손자는 기다릴 부모가 없다(3.8의 2번).
            orphaned.extend(
                await cancel_orphans(session, workspace_id, [run.id for run in failed_runs], now)
            )
            # 실패한 런이 자식이었으면 기다리던 부모가 그 실패 결과로 이어 간다(3.8의 3번).
            for run in failed_runs:
                claimed = await claim_parent(session, workspace_id, run, now)
                if claimed is not None:
                    resumed.append(claimed)
            await session.flush()
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
    for ref, completed in orphaned:
        await bus.publish(ref, "run.completed", completed)
        bus.end(ref.run_id)
    for claimed in resumed:
        await bus.publish(claimed.ref, "child_run.completed", child_completed_data(claimed))
    for position, ref in enumerate(requeue, start=1):
        # 채널을 미리 열어 두어 재기동 직후 붙는 화면이 실행을 따라갈 수 있게 한다.
        await bus.publish(ref, "run.queued", {"position": position})
        supervisor.enqueue(_run_key(ref))
    result = {
        "failed": len(failed),
        "orphaned": len(orphaned),
        "resumed_parents": len(resumed),
        "requeued": len(requeue),
        "released": released,
    }
    if any(result.values()):
        log.warning("runner.recovered", **result)
    return result
