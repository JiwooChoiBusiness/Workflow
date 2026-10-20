# pyright: strict
"""노드 러너 인터페이스와 레지스트리(설계 3.11).

엔진은 노드 타입을 모른다. 타입별 차이(설정 모델, 출력, 분기 핸들, 변수 참조, 실행)는 NodeRunner가
알려 주고, 오류 정책·재시도·타임아웃·마스킹·체크포인트는 엔진이 run() 바깥에서 처리한다. 노드는
NodeResult를 돌려주거나 NodeFailure를 던진다.
"""

import builtins
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, Field

from app.schemas.events import UsageTotal, WorkflowFormData
from app.schemas.run import PendingInteractionOut
from app.schemas.workflow import (
    OUT_HANDLE,
    ErrorStrategy,
    Issue,
    Node,
    NodeError,
    NodeType,
    OutputSpecOut,
    VarType,
    WorkflowKind,
)
from app.workflow.memory import Turn
from app.workflow.ports import CancelToken, EnginePorts
from app.workflow.variables import PoolView

ALL_KINDS: frozenset[WorkflowKind] = frozenset({"workflow", "chatflow"})
WORKFLOW_ONLY: frozenset[WorkflowKind] = frozenset({"workflow"})
CHATFLOW_ONLY: frozenset[WorkflowKind] = frozenset({"chatflow"})
ALL_STRATEGIES: frozenset[ErrorStrategy] = frozenset({"fail", "default", "branch"})
# 제어 노드는 fail만 받는다(2.2).
ONLY_FAIL: frozenset[ErrorStrategy] = frozenset({"fail"})
# 노드가 ctx.emit으로 낼 수 있는 이벤트. 시작·완료·재시도 같은 나머지는 엔진만 낸다. 모델 호출
# 사용량은 1단계 usage와 이름이 겹치지 않게 node.usage다(data에 step이 없고 노드가 붙는다).
NODE_EVENT_TYPES = frozenset(
    {"node.text_delta", "node.progress", "node.waiting", "answer.delta", "node.usage"}
)
NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
# 재개할 때 엔진이 기다리던 노드의 state에 사람의 응답(human_input 폼)을 넣는 키.
REPLY_STATE_KEY = "reply"


@dataclass(frozen=True, slots=True)
class OutputSpec:
    name: str
    type: VarType


@dataclass(frozen=True, slots=True)
class VarRef:
    """config 안의 구조화 변수 참조. 검증과 단일 노드 실행의 입력 목록이 쓴다."""

    field: str
    selector: str
    # 런타임에 ctx.resolve(selector, expect)로 읽는 타입. 검증의 type_mismatch 경고에 쓴다.
    expect: VarType | None = None
    # 이 노드의 하위 그래프 안에서 읽는 참조(loop의 종료 조건). 하위 노드 출력과 반복 변수도 된다.
    inner: bool = False


@dataclass(frozen=True, slots=True)
class TemplateField:
    field: str
    text: str
    # None이면 변수 풀이 컨텍스트다({{ llm_1.text }}). 아니면 이 이름만 쓴다(template 노드의 vars).
    names: frozenset[str] | None = None


class NodeFailure(Exception):
    """노드 안에서 던지면 그 노드의 실패로 기록된다. retryable이면 retry 정책을 탄다."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class NodeNotImplemented(NodeFailure):
    """레지스트리에 자리만 있는 노드. 오류 정책·재시도와 관계없이 런이 실패한다."""

    def __init__(self, node_type: str, milestone: str) -> None:
        super().__init__(
            "node_not_implemented", f"{node_type} 노드는 아직 실행할 수 없다({milestone}에서 구현)"
        )


def duplicate_names(names: Iterable[str], field: str) -> list[Issue]:
    # 같은 이름이 두 번째로 나온 자리마다 오류. field에는 "outputs[{}].name"처럼 index 자리를 둔다.
    seen: set[str] = set()
    issues: list[Issue] = []
    for index, name in enumerate(names):
        if name in seen:
            issues.append(
                Issue(
                    level="error",
                    code="duplicate_name",
                    message=f"이름 {name}이(가) 겹친다",
                    field=field.format(index),
                )
            )
        seen.add(name)
    return issues


# cancelled는 런이 멈추는 사이에 끝난 container 노드(iteration)만 쓴다.
NodeResultStatus = Literal["succeeded", "failed", "waiting_input", "skipped", "cancelled"]


class NodeResult(BaseModel):
    status: NodeResultStatus
    # 선언한 출력 이름만. 엔진이 선언 타입으로 맞춘 뒤 풀에 넣는다.
    outputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    # 분기 노드가 고른 핸들.
    branch: str | None = None
    error: NodeError | None = None
    usage: UsageTotal | None = None
    # 노드 런 process 열(모델 id, 상태 코드 등).
    process: dict[str, Any] = Field(default_factory=dict[str, Any])
    warnings: list[str] = Field(default_factory=list[str])
    # waiting_input일 때 기다리는 것. agent 노드는 자식 런의 승인·질문, human_input은 폼이다.
    pending: PendingInteractionOut | WorkflowFormData | None = None
    # 재개 때 run()의 state로 돌려받는 값(child_run_id 등).
    state: dict[str, Any] = Field(default_factory=dict[str, Any])
    # 여기부터는 설계 3.11에 더한 자리다. 노드 타입을 모르는 엔진이 타입별 효과를 받는다.
    # 노드 런 inputs. None이면 엔진이 ctx.resolve로 읽은 값을 남긴다.
    inputs: dict[str, Any] | None = None
    # end 노드: 런 출력에 합칠 값.
    run_outputs: dict[str, Any] | None = None
    # answer 노드: 런의 답 텍스트에 이어 붙일 값.
    answer: str | None = None
    # variable_assigner: 대화 변수의 새 값. 노드가 성공했을 때만 반영한다.
    conv_updates: dict[str, Any] = Field(default_factory=dict[str, Any])
    # variable_assigner: 이 노드를 품은 loop의 반복 변수 이름 → 새 값. 성공했을 때만 반영한다.
    loop_updates: dict[str, Any] = Field(default_factory=dict[str, Any])
    child_run_id: uuid.UUID | None = None
    child_session_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class FramePlan:
    """container 노드(iteration)가 엔진에 맡기는 하위 그래프 반복(3.5)."""

    items: Sequence[Any]
    # 반복마다 모을 하위 그래프 노드 출력의 선택자(output_var).
    output: str
    # 동시에 도는 반복 수. 1이면 순차다.
    parallel: int = 1
    # 첫 실패에서 남은 반복을 멈춘다(error_mode=terminate).
    stop_on_failure: bool = True


class FrameResult(BaseModel):
    index: int
    status: Literal["succeeded", "failed"]
    value: Any = None
    error: NodeError | None = None
    # 실패한 반복에서 처음 실패한 하위 노드.
    node_id: str | None = None


class FramesOutcome(BaseModel):
    """반복 결과. waiting_input이면 state를 NodeResult.state로 돌려주면 재개 때 이어 간다.

    cancelled는 런이 멈춰(중지·실패·시간 초과) 반복을 끝내지 못한 것이다.
    """

    status: Literal["succeeded", "failed", "waiting_input", "cancelled"]
    # 끝난 반복만, index 순.
    results: list[FrameResult] = Field(default_factory=list[FrameResult])
    # failed일 때 iteration을 멈춘 첫 실패 반복(stop_on_failure).
    failure: FrameResult | None = None
    state: dict[str, Any] = Field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class LoopPlan:
    """loop 노드가 엔진에 맡기는 반복(설계 2.4, 3.5).

    반복 변수는 처음 시작할 때 풀의 "<loop id>" 자리에 넣고, 하위 그래프 안의 variable_assigner가
    바꾼다. 반복 하나가 끝날 때마다 until을 불러 참이면 멈추고, max_rounds번 돌면 멈춘다.
    """

    # 반복 변수의 처음 값. 재개할 때는 풀에 남은 지금 값을 이어 쓰므로 비워 둔다.
    variables: Mapping[str, Any]
    types: Mapping[str, VarType | None]
    max_rounds: int
    # 끝난 반복의 index가 적용된 풀 뷰를 받는다. NodeFailure를 던지면 loop가 실패한다.
    until: Callable[[PoolView], bool] | None = None


class LoopOutcome(BaseModel):
    """loop 반복 결과. waiting_input이면 state를 NodeResult.state로 돌려주면 재개 때 이어 간다."""

    status: Literal["succeeded", "failed", "waiting_input", "cancelled"]
    # 끝난 반복 수.
    rounds: int = 0
    # 반복 변수의 마지막 값(succeeded일 때).
    variables: dict[str, Any] = Field(default_factory=dict[str, Any])
    # condition(종료 조건이 참), limit(max_rounds에 닿음).
    stopped: Literal["condition", "limit"] | None = None
    failure: FrameResult | None = None
    state: dict[str, Any] = Field(default_factory=dict[str, Any])


class NodeContext(Protocol):
    run_id: uuid.UUID
    node_id: str
    node_run_id: uuid.UUID
    attempt: int
    kind: WorkflowKind
    iteration_index: int | None
    # 읽기 전용 풀. 현재 iteration index가 적용된 해석기다.
    pool: PoolView
    ports: EnginePorts
    cancel: CancelToken
    # 런 입력(start 노드가 쓴다).
    run_inputs: Mapping[str, Any]
    # 챗플로우 대화의 앞 턴들(오래된 것부터). 메모리를 켠 llm 노드가 쓴다.
    history: Sequence[Turn]
    # answer 노드가 실행되기 전에 엔진이 이미 answer.delta로 흘린 글(3.8의 미리 흘리기).
    streamed: str

    # NODE_EVENT_TYPES만. node_id·node_run_id는 엔진이 붙인다.
    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None: ...

    # variables가 없으면 변수 풀이 컨텍스트다. 없는 변수는 빈칸이 되고 missing_variable 경고가
    # 남는다.
    def render(self, template: str, variables: Mapping[str, Any] | None = None) -> str: ...

    # 3.2의 런타임 변환을 거친 값. skipped 노드는 null, 변환할 수 없으면 NodeFailure(type_mismatch).
    def resolve(self, selector: str, expect: VarType | None = None) -> Any: ...

    def is_skipped(self, selector: str) -> bool: ...

    def warn(self, message: str) -> None: ...

    # 이 노드의 하위 그래프(parent_id가 이 노드)를 plan.items마다 돈다. container 노드만 부른다.
    # state는 앞 구간이 waiting_input으로 돌려준 FramesOutcome.state다.
    async def run_frames(
        self, plan: FramePlan, state: Mapping[str, Any] | None
    ) -> FramesOutcome: ...

    # loop 노드만 부른다. state는 앞 구간이 waiting_input으로 돌려준 LoopOutcome.state다.
    async def run_loop(self, plan: LoopPlan, state: Mapping[str, Any] | None) -> LoopOutcome: ...


class GraphView(Protocol):
    """NodeRunner.validate가 보는 그래프. 출력 타입은 검증을 통과한 config 기준이다."""

    @property
    def kind(self) -> WorkflowKind: ...

    # 지금 검증하는 노드.
    @property
    def node_id(self) -> str: ...

    def node(self, node_id: str) -> Node | None: ...

    # None이면 그 노드의 출력을 모른다(config 오류 등). 그때 참조는 따지지 않는다.
    def outputs_of(self, node_id: str) -> Mapping[str, VarType] | None: ...

    def output_type(self, selector: str) -> VarType | None: ...

    # container 노드가 돌고 있는 동안 하위 그래프에 보이는 값(loop의 반복 변수). 없으면 None.
    def inner_outputs(self, node_id: str) -> Mapping[str, VarType] | None: ...

    # 대화 변수 목록을 모르면 None.
    def conversation_vars(self) -> Mapping[str, VarType] | None: ...

    def children(self, iteration_id: str) -> list[Node]: ...

    # 워크스페이스에 살아 있는 에이전트인지. 검증하는 쪽이 에이전트 목록을 주지 않았으면 None이다
    # (엔진처럼 DB를 모르는 곳).
    def agent_known(self, agent_id: uuid.UUID) -> bool | None: ...


class NodeRunner[C: BaseModel]:
    type: ClassVar[str]
    version: ClassVar[int] = 1
    # 클래스 본문의 type은 위의 노드 타입 이름이라 내장 type을 이름으로 가리킨다.
    Config: builtins.type[C]
    kinds: ClassVar[frozenset[WorkflowKind]] = ALL_KINDS
    # kinds 밖이어도 loop 하위 그래프 안에서는 둘 수 있는 앱 종류(variable_assigner).
    loop_kinds: ClassVar[frozenset[WorkflowKind]] = frozenset()
    error_strategies: ClassVar[frozenset[ErrorStrategy]] = ONLY_FAIL
    implemented: ClassVar[bool] = True
    # config와 관계없는 출력. outputs()의 기본값이고 노드 타입 목록에 그대로 나간다.
    fixed_outputs: ClassVar[tuple[OutputSpec, ...]] = ()
    dynamic_outputs: ClassVar[bool] = False
    branching: ClassVar[bool] = False
    # iteration처럼 하위 그래프를 도는 노드는 동시 실행 자리를 차지하지 않는다(하위 노드가 쓴다).
    container: ClassVar[bool] = False
    # 같은 타입끼리의 동시 실행 상한(code 노드 1, 3.4). None이면 전역 상한만 따른다.
    concurrency: ClassVar[int | None] = None
    # False면 usage를 런 사용량에 더하지 않는다(agent 노드, 3.10). 토큰 예산에는 들어간다.
    counts_usage: ClassVar[bool] = True
    # 대화 변수를 바꾸는 노드(variable_assigner). answer 미리 흘리기가 conv 값을 언제 믿을지 본다.
    writes_conversation: ClassVar[bool] = False

    def parse_config(self, raw: Mapping[str, Any]) -> C:
        return self.Config.model_validate(dict(raw))

    def outputs(self, config: C) -> list[OutputSpec]:
        return list(self.fixed_outputs)

    # container 노드가 돌고 있는 동안 하위 그래프 노드가 "<이 노드 id>.<이름>"으로 읽는 값.
    def inner_outputs(self, config: C) -> list[OutputSpec] | None:
        return None

    # 출발 핸들. 엔진이 error.strategy=branch면 fail을 더한다.
    def handles(self, config: C) -> list[str]:
        return [OUT_HANDLE]

    def var_refs(self, config: C) -> list[VarRef]:
        return []

    def template_fields(self, config: C) -> list[TemplateField]:
        return []

    # 답을 흘리는 노드(answer)의 템플릿. 엔진이 미리 흘리기를 이 템플릿으로 정한다(3.8).
    def answer_template(self, config: C) -> str | None:
        return None

    # node.text_delta를 이어 붙이면 그대로 이 이름의 출력이 되는 노드(llm의 text). 그런 노드만
    # answer가 델타를 미리 흘릴 수 있다.
    def stream_output(self, config: C) -> str | None:
        return None

    # 참조·타입 검사(graph.py) 밖의 타입별 규칙.
    def validate(self, config: C, graph: GraphView) -> list[Issue]:
        return []

    def timeout_sec(self, config: C) -> float | None:
        return None

    async def run(self, ctx: NodeContext, config: C, state: dict[str, Any] | None) -> NodeResult:
        raise NotImplementedError


AnyRunner = NodeRunner[Any]


class NodeRegistry:
    """(type, version) → 러너."""

    def __init__(self, runners: Iterable[AnyRunner] = ()) -> None:
        self._runners: dict[tuple[str, int], AnyRunner] = {}
        for runner in runners:
            self.register(runner)

    def register(self, runner: AnyRunner) -> None:
        self._runners[(runner.type, runner.version)] = runner

    def get(self, node_type: str, version: int = 1) -> AnyRunner | None:
        return self._runners.get((node_type, version))

    def has_type(self, node_type: str) -> bool:
        return any(key[0] == node_type for key in self._runners)

    def latest_version(self, node_type: str) -> int | None:
        versions = [version for kind, version in self._runners if kind == node_type]
        return max(versions) if versions else None

    def copy(self) -> "NodeRegistry":
        return NodeRegistry(self._runners.values())

    def __iter__(self) -> Iterator[AnyRunner]:
        return iter(self._runners.values())

    def describe(self) -> list[NodeType]:
        order: tuple[ErrorStrategy, ...] = ("fail", "default", "branch")
        return [
            NodeType(
                type=runner.type,
                version=runner.version,
                kinds=sorted(runner.kinds),
                implemented=runner.implemented,
                error_strategies=[s for s in order if s in runner.error_strategies],
                config_schema=runner.Config.model_json_schema(),
                outputs=[OutputSpecOut(name=o.name, type=o.type) for o in runner.fixed_outputs],
                dynamic_outputs=runner.dynamic_outputs,
                branching=runner.branching,
                loop_kinds=sorted(runner.loop_kinds),
            )
            for runner in self._runners.values()
        ]
