# pyright: strict
"""agent 노드(설계 2.3, 3.7, 3.8, 3.10, 오케스트레이션 9장).

1단계 과업 런 하나를 자식으로 만들고 끝나기를 기다린다. 자식은 에이전트 레인에서 돌고 이 노드는
워크플로우 레인에서 기다린다. 자식이 승인·질문으로 멈추면 노드도 waiting_input으로 끝나며 자식 런
id를 state에 남긴다. 재개하면 새 자식을 만들지 않고 그 자식을 다시 기다린다. 그사이 자식이 이미
끝났으면 기다리지 않고 그 결과를 쓴다. 승인을 거절해도 자식은 계속 돌고 그 결과가 출력이 된다.

instruction은 다른 노드의 글자 필드처럼 변수 풀로 렌더해 넘기고 과업 서비스는 다시 채우지 않는다
(값 속 {{ }}가 템플릿으로 해석되지 않게, 9.3). inputs는 task/inputs.json과 첫 메시지의 입력 요약이
되고, files_var와 inputs 안의 파일은 자식 작업공간 task/files/로 복사된다.

자식의 이벤트는 워크플로우 스트림에 싣지 않는다(3.8). 자식을 만들자마자, 그리고 10초마다
node.progress(자식 런·세션 id, 스텝, 사용량)를 내고 자식이 큐에 있으면 node.waiting도 낸다. 자식의
사용량은 런 사용량에 더하지 않고 토큰 예산에만 센다(3.10).
"""

import asyncio
import contextlib
import uuid
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.agent.task_service import (
    AgentTaskResult,
    AgentTaskSpec,
    TaskSpecError,
    check_output_schema,
)
from app.schemas.agent_version import ResultSpec
from app.schemas.common import RunLimits
from app.schemas.events import NodeError
from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NAME_PATTERN,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
    VarRef,
    duplicate_names,
)
from app.workflow.ports import ChildRunView, TaskPort, TaskPortError
from app.workflow.variables import FileRef, is_file

PROGRESS_SEC = 10.0
CANCEL_TIMEOUT_SEC = 5.0
# 오케스트레이션 3.9의 전달 파일 한도와 같다.
MAX_FILES = 20
MAX_FILES_BYTES = 200 * 1024 * 1024


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    var: str


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    # None이면 발행 버전. 초안으로 도는 런(미리보기·단일 노드 실행)은 발행 버전이 없으면 초안을
    # 쓴다(오케스트레이션 2.4와 같은 규칙).
    agent_version_id: uuid.UUID | None = None
    # 템플릿(3.3). 렌더한 글이 과업 지시가 된다.
    instruction: str = ""
    inputs: list[AgentInput] = Field(default_factory=list[AgentInput])
    # file 또는 array[file] 변수. 자식 작업공간 task/files/로 간다.
    files_var: str | None = None
    # 결과 형식(오케스트레이션 2.2). 노드는 모델이 없으므로 json이면 schema가 있어야 한다.
    result: ResultSpec = Field(default_factory=ResultSpec)
    # 준 필드만 에이전트 버전의 한도를 덮어쓴다.
    limits: RunLimits | None = None
    # off면 자식의 승인을 모두 끈다. 버전 settings.approval_mode=off도 같은 효과다(9.1).
    approval_mode: Literal["inherit", "off"] = "inherit"
    # per_conversation은 챗플로우에서 대화마다 과업 세션을 이어 쓴다(M17). 그 전에는 ephemeral이다.
    session_mode: Literal["ephemeral", "per_conversation"] = "ephemeral"


def _file_refs(value: object) -> list[dict[str, Any]] | None:
    """file 또는 file 배열이면 그 참조들. null은 빈 목록, 그 밖의 값은 None."""
    if value is None:
        return []
    if is_file(value):
        return [cast(dict[str, Any], value)]
    if isinstance(value, list):
        items = cast(list[Any], value)
        if all(is_file(item) for item in items):
            return [cast(dict[str, Any], item) for item in items]
    return None


async def _cancel_quietly(tasks: TaskPort, child: uuid.UUID) -> None:
    # 노드가 취소되는 중이라 오래 붙잡지 않는다. 남은 자식은 실행기가 런 끝에 다시 정리한다.
    with contextlib.suppress(Exception):
        async with asyncio.timeout(CANCEL_TIMEOUT_SEC):
            await tasks.cancel(child)


class AgentRunner(NodeRunner[AgentConfig]):
    type = "agent"
    Config = AgentConfig
    error_strategies = ALL_STRATEGIES
    counts_usage = False
    fixed_outputs = (
        OutputSpec("text", "string"),
        OutputSpec("result", "object"),
        OutputSpec("files", "array[file]"),
        OutputSpec("status", "string"),
        OutputSpec("usage", "object"),
        OutputSpec("child_run_id", "string"),
    )

    def var_refs(self, config: AgentConfig) -> list[VarRef]:
        refs = [VarRef(f"inputs[{i}].var", item.var) for i, item in enumerate(config.inputs)]
        if config.files_var:
            refs.append(VarRef("files_var", config.files_var))
        return refs

    def template_fields(self, config: AgentConfig) -> list[TemplateField]:
        return [TemplateField("instruction", config.instruction)]

    def validate(self, config: AgentConfig, graph: GraphView) -> list[Issue]:
        issues = duplicate_names((item.name for item in config.inputs), "inputs[{}].name")
        if graph.agent_known(config.agent_id) is False:
            issues.append(
                Issue(
                    level="error",
                    code="agent_not_found",
                    message="에이전트를 찾을 수 없다. 지워졌거나 다른 PC에서 가져온 노드다",
                    field="agent_id",
                )
            )
        if not config.instruction.strip():
            issues.append(
                Issue(
                    level="error",
                    code="instruction_missing",
                    message="에이전트에게 줄 지시(instruction)를 쓴다",
                    field="instruction",
                )
            )
        result = config.result
        if result.format == "json":
            if result.json_schema is None:
                issues.append(
                    Issue(
                        level="error",
                        code="output_schema_required",
                        message="결과 형식이 json이면 결과 JSON Schema를 정해야 한다",
                        field="result.schema",
                    )
                )
            else:
                try:
                    check_output_schema(result.json_schema)
                except TaskSpecError as exc:
                    issues.append(
                        Issue(
                            level="error", code=exc.code, message=exc.message, field="result.schema"
                        )
                    )
        if config.session_mode == "per_conversation" and graph.kind != "chatflow":
            issues.append(
                Issue(
                    level="warning",
                    code="session_mode_ignored",
                    message="per_conversation은 챗플로우에서만 쓴다. 여기서는 ephemeral로 돈다",
                    field="session_mode",
                )
            )
        return issues

    async def run(
        self, ctx: NodeContext, config: AgentConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        tasks = ctx.ports.tasks
        if tasks is None:
            raise NodeFailure("tasks_unavailable", "과업 서비스가 없어 에이전트를 부를 수 없다")
        instruction = ctx.render(config.instruction)
        if not instruction.strip():
            raise NodeFailure("instruction_invalid", "채운 instruction이 비어 있다")
        inputs = {item.name: ctx.resolve(item.var) for item in config.inputs}
        files = self._files(ctx, config, inputs)
        if config.session_mode == "per_conversation":
            ctx.warn("session_mode_ignored: per_conversation은 아직 ephemeral로 돈다")
        shown: dict[str, Any] = {
            "agent_id": str(config.agent_id),
            "agent_version_id": str(config.agent_version_id) if config.agent_version_id else None,
            "instruction": instruction,
            "inputs": inputs,
            "files": [str(ref.get("filename") or "") for ref in files],
        }
        child = _resumed_child(state)
        try:
            if child is None:
                child = await tasks.start(self._spec(ctx, config, instruction, inputs, files))
            view = await tasks.child(child)
            await _report(ctx, view)
            result = await _follow(ctx, tasks, child)
            if result.status == "succeeded" and config.result.files and result.files:
                view = await tasks.child(child)
        except TaskPortError as exc:
            raise NodeFailure(exc.code, exc.message) from None
        except asyncio.CancelledError:
            # 런 중지·다른 가지의 실패로 노드가 멈추면 자식도 끝낸다. 서버 종료면 복구가 맡는다.
            if child is not None and ctx.cancel.reason != "shutdown":
                await _cancel_quietly(tasks, child)
            raise
        return self._result(config, child, view, result, shown)

    def _files(
        self, ctx: NodeContext, config: AgentConfig, inputs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        if config.files_var:
            found = _file_refs(ctx.resolve(config.files_var))
            if found is None:
                raise NodeFailure("type_mismatch", "files_var는 파일이나 파일 배열이어야 한다")
            files.extend(found)
        # inputs 안의 파일도 자식 작업공간으로 함께 간다(과업 서비스가 경로로 바꾼다).
        attached = [ref for value in inputs.values() for ref in (_file_refs(value) or [])]
        if len(files) + len(attached) > MAX_FILES:
            raise NodeFailure("file_limit", f"자식에게 넘길 파일은 {MAX_FILES}개까지다")
        size = sum(int(ref.get("size_bytes") or 0) for ref in [*files, *attached])
        if size > MAX_FILES_BYTES:
            raise NodeFailure(
                "file_too_large", f"자식에게 넘길 파일은 합쳐서 {MAX_FILES_BYTES // 2**20}MB까지다"
            )
        return files

    def _spec(
        self,
        ctx: NodeContext,
        config: AgentConfig,
        instruction: str,
        inputs: dict[str, Any],
        files: list[dict[str, Any]],
    ) -> AgentTaskSpec:
        schema = config.result.json_schema if config.result.format == "json" else None
        return AgentTaskSpec(
            # 포트가 이 런의 워크스페이스로 바꾼다.
            workspace_id=uuid.UUID(int=0),
            agent_id=config.agent_id,
            agent_version_id=config.agent_version_id,
            inputs=inputs,
            instruction=instruction,
            output_schema=schema,
            files=[FileRef.model_validate(ref) for ref in files],
            limits=config.limits,
            approval_mode="off" if config.approval_mode == "off" else None,
            session_mode="ephemeral",
            correlation={
                "node_run_id": str(ctx.node_run_id),
                "node_id": ctx.node_id,
                "iteration_index": ctx.iteration_index,
            },
        )

    def _result(
        self,
        config: AgentConfig,
        child: uuid.UUID,
        view: ChildRunView,
        result: AgentTaskResult,
        shown: dict[str, Any],
    ) -> NodeResult:
        session = view.session_id
        process: dict[str, Any] = {
            "agent_id": str(config.agent_id),
            "child_run_id": str(child),
            "child_session_id": str(session),
            "child_status": result.status,
            "steps": view.steps,
        }
        common: dict[str, Any] = {
            "child_run_id": child,
            "child_session_id": session,
            "inputs": shown,
            "process": process,
        }
        if result.status == "waiting_input":
            if result.pending_interaction is None:
                raise NodeFailure(
                    "invalid_child_state", "자식 런이 입력을 기다리는데 승인·질문 내용이 없다"
                )
            # 사용량은 끝난 구간에서 한 번만 센다. 대기 구간에서 내면 재개 뒤 두 번 들어간다.
            return NodeResult(
                status="waiting_input",
                pending=result.pending_interaction,
                state={"child_run_id": str(child), "child_session_id": str(session)},
                **common,
            )
        if result.status == "succeeded":
            files = view.files if config.result.files else []
            return NodeResult(
                status="succeeded",
                outputs={
                    "text": result.text,
                    "result": result.result,
                    "files": files,
                    "status": result.status,
                    "usage": result.usage.model_dump(),
                    "child_run_id": str(child),
                },
                usage=result.usage,
                **common,
            )
        error = result.error
        code = error.code if error is not None else result.status
        message = (
            error.message
            if error is not None and error.message
            else f"자식 런이 {result.status}로 끝났다"
        )
        return NodeResult(
            status="failed",
            error=NodeError(
                code=code, message=message, retryable=error.retryable if error else False
            ),
            usage=result.usage,
            **common,
        )


def _resumed_child(state: dict[str, Any] | None) -> uuid.UUID | None:
    raw = (state or {}).get("child_run_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise NodeFailure("invalid_state", "재개할 자식 런 id를 읽지 못했다") from None


async def _report(ctx: NodeContext, view: ChildRunView) -> None:
    if view.status == "queued" and view.position is not None:
        await ctx.emit("node.waiting", {"reason": "queue", "position": view.position})
    await ctx.emit(
        "node.progress",
        {
            "child_run_id": view.run_id,
            "child_session_id": view.session_id,
            "steps": view.steps,
            "usage": view.usage.model_dump(),
            "position": view.position,
        },
    )


async def _follow(ctx: NodeContext, tasks: TaskPort, child: uuid.UUID) -> AgentTaskResult:
    """자식이 끝나거나 입력을 기다릴 때까지 기다리며 10초마다 진행을 알린다."""
    waiter = asyncio.ensure_future(tasks.wait(child))
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=PROGRESS_SEC)
            if waiter in done:
                return waiter.result()
            await _report(ctx, await tasks.child(child))
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.wait({waiter})
