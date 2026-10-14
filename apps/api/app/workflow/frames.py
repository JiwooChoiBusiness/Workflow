# pyright: strict
"""실행 프레임과 노드·엣지 상태 기계(설계 3.4, 3.5, 3.7).

프레임은 루트 그래프 한 번, 또는 iteration 하위 그래프의 한 반복이다. 상태 키는 노드·엣지 id에
"@<index>"를 붙여 반복끼리 구분한다(루트는 붙이지 않는다). 노드가 끝나면 FrameState.complete()가
나가는 엣지를 활성·비활성으로 해결하고, 들어오는 엣지가 모두 해결된 노드를 준비(하나 이상 활성)나
skipped(모두 비활성)로 넘긴다. skipped는 나가는 엣지를 모두 비활성으로 만들며 그 자리에서 전파한다.

반복 프레임은 iteration_start를 실행하지 않는다. 엔진이 item·index를 풀에 넣고 enter()로 그
자리를 끝낸 뒤 이어지는 노드부터 돈다. 반복 안의 노드가 오류 정책 fail로 실패하면 런이 아니라 그
반복이 실패하고(failure), iteration의 error_mode가 나머지를 정한다.
"""

import uuid
from collections import deque
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.events import NodeError, UsageTotal
from app.schemas.workflow import OUT_HANDLE, Edge, Graph

NodeState = Literal[
    "pending", "running", "succeeded", "failed", "skipped", "cancelled", "waiting_input"
]
EdgeState = Literal["pending", "active", "inactive"]
# 나가는 엣지가 해결된(다시 돌지 않는) 상태.
SETTLED: frozenset[NodeState] = frozenset({"succeeded", "failed", "skipped"})

START_TYPE = "start"
ITERATION_TYPE = "iteration"
ITERATION_START_TYPE = "iteration_start"


def state_key(item_id: str, index: int | None = None) -> str:
    return item_id if index is None else f"{item_id}@{index}"


@dataclass(frozen=True)
class FrameGraph:
    """프레임 하나의 고정 구조. iteration_id가 None이면 루트다."""

    iteration_id: str | None
    # start 또는 iteration_start. 없으면(검증 오류) 아무것도 돌지 않는다.
    entry: str | None
    nodes: tuple[str, ...]
    edges: tuple[Edge, ...]
    incoming: Mapping[str, tuple[Edge, ...]]
    outgoing: Mapping[str, tuple[Edge, ...]]


def build_frames(graph: Graph) -> dict[str | None, FrameGraph]:
    """parent_id로 노드를 나누고 같은 프레임 안의 엣지만 붙인다. 프레임을 넘는 엣지는 버린다."""
    members: dict[str | None, list[str]] = {None: []}
    parents: dict[str, str | None] = {}
    entries: dict[str | None, str] = {}
    for node in graph.nodes:
        if node.id in parents:
            continue
        parents[node.id] = node.parent_id
        members.setdefault(node.parent_id, []).append(node.id)
        if node.type in (START_TYPE, ITERATION_START_TYPE) and node.parent_id not in entries:
            entries[node.parent_id] = node.id
    edges: dict[str | None, list[Edge]] = {}
    for edge in graph.edges:
        if edge.source not in parents or edge.target not in parents:
            continue
        frame = parents[edge.source]
        if parents[edge.target] != frame:
            continue
        edges.setdefault(frame, []).append(edge)
    frames: dict[str | None, FrameGraph] = {}
    for frame, node_ids in members.items():
        frame_edges = tuple(edges.get(frame, ()))
        incoming: dict[str, list[Edge]] = {node_id: [] for node_id in node_ids}
        outgoing: dict[str, list[Edge]] = {node_id: [] for node_id in node_ids}
        for edge in frame_edges:
            outgoing[edge.source].append(edge)
            incoming[edge.target].append(edge)
        frames[frame] = FrameGraph(
            iteration_id=frame,
            entry=entries.get(frame),
            nodes=tuple(node_ids),
            edges=frame_edges,
            incoming={key: tuple(value) for key, value in incoming.items()},
            outgoing={key: tuple(value) for key, value in outgoing.items()},
        )
    return frames


def single_frame(node_id: str) -> FrameGraph:
    """단일 노드 실행(6.1)의 프레임. 노드 하나뿐이라 끝나도 다음 노드가 준비되지 않는다."""
    return FrameGraph(
        iteration_id=None,
        entry=node_id,
        nodes=(node_id,),
        edges=(),
        incoming={node_id: ()},
        outgoing={node_id: ()},
    )


@dataclass
class Transition:
    ready: list[str] = field(default_factory=list[str])
    skipped: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class FrameFailure:
    """반복 프레임을 멈춘 첫 실패. 엔진이 런 대신 이 반복을 실패로 끝낸다."""

    node_id: str
    error: NodeError


class FrameState:
    """프레임 한 번 실행의 상태. 같은 FrameGraph로 반복마다 하나씩 만든다."""

    def __init__(self, graph: FrameGraph, index: int | None = None) -> None:
        self.graph = graph
        self.index = index
        self.nodes: dict[str, NodeState] = dict.fromkeys(graph.nodes, "pending")
        self.edges: dict[str, EdgeState] = {edge.id: "pending" for edge in graph.edges}
        self._unresolved = {node_id: len(graph.incoming[node_id]) for node_id in graph.nodes}
        self._active = dict.fromkeys(graph.nodes, 0)
        self.failure: FrameFailure | None = None

    def key(self, node_id: str) -> str:
        return state_key(node_id, self.index)

    @property
    def frame_key(self) -> str:
        """하위 프레임 표의 키("<iteration id>@<index>"). 루트는 빈 문자열이다."""
        if self.graph.iteration_id is None:
            return ""
        return state_key(self.graph.iteration_id, self.index)

    @property
    def waiting(self) -> bool:
        return any(state == "waiting_input" for state in self.nodes.values())

    def begin(self) -> list[str]:
        entry = self.graph.entry
        return [entry] if entry is not None and self.nodes.get(entry) == "pending" else []

    def enter(self) -> Transition:
        """반복 프레임의 iteration_start를 실행 없이 끝낸다. 값은 엔진이 먼저 풀에 넣는다."""
        entry = self.graph.entry
        if entry is None or self.nodes.get(entry) != "pending":
            return Transition()
        return self.complete(entry, "succeeded", (OUT_HANDLE,))

    def mark(self, node_id: str, state: NodeState) -> None:
        self.nodes[node_id] = state

    def complete(self, node_id: str, state: NodeState, handles: Collection[str]) -> Transition:
        """node_id를 state로 끝내고 handles에 해당하는 나가는 엣지만 활성으로 해결한다.

        succeeded·failed(오류 정책으로 계속)·skipped만 엣지를 해결한다. skipped는 handles를 무시하고
        전부 비활성이다.
        """
        self.nodes[node_id] = state
        result = Transition()
        queue: deque[tuple[str, Collection[str]]] = deque([(node_id, handles)])
        while queue:
            current, active_handles = queue.popleft()
            if self.nodes[current] == "skipped":
                active_handles = ()
            for edge in self.graph.outgoing[current]:
                if self.edges[edge.id] != "pending":
                    continue
                active = edge.source_handle in active_handles
                self.edges[edge.id] = "active" if active else "inactive"
                target = edge.target
                self._unresolved[target] -= 1
                if active:
                    self._active[target] += 1
                if self._unresolved[target] > 0 or self.nodes[target] != "pending":
                    continue
                if self._active[target] > 0:
                    result.ready.append(target)
                else:
                    self.nodes[target] = "skipped"
                    result.skipped.append(target)
                    queue.append((target, ()))
        return result

    def snapshot(self) -> tuple[dict[str, str], dict[str, str]]:
        nodes = {
            self.key(node_id): state for node_id, state in self.nodes.items() if state != "pending"
        }
        edges = {
            state_key(edge_id, self.index): state
            for edge_id, state in self.edges.items()
            if state != "pending"
        }
        return nodes, edges

    def restore(self, nodes: Mapping[str, str], edges: Mapping[str, str]) -> list[str]:
        """체크포인트의 상태를 되돌리고 다시 돌릴 노드를 돌려준다.

        waiting_input 노드는 저장해 둔 상태(NodeResult.state)로 다시 돈다. 체크포인트 시점에
        running·cancelled였던 노드(실패 런)도 다시 돌릴 대상이다.
        """
        for edge in self.graph.edges:
            state = edges.get(state_key(edge.id, self.index))
            if state in ("active", "inactive"):
                self.edges[edge.id] = state
        rerun: list[str] = []
        for node_id in self.graph.nodes:
            state = nodes.get(self.key(node_id))
            if state in SETTLED:
                self.nodes[node_id] = state
            elif state in ("waiting_input", "running", "cancelled"):
                rerun.append(node_id)
        for node_id in self.graph.nodes:
            incoming = self.graph.incoming[node_id]
            self._unresolved[node_id] = sum(1 for e in incoming if self.edges[e.id] == "pending")
            self._active[node_id] = sum(1 for e in incoming if self.edges[e.id] == "active")
            if (
                self.nodes[node_id] == "pending"
                and node_id not in rerun
                and incoming
                and self._unresolved[node_id] == 0
                and self._active[node_id] > 0
            ):
                rerun.append(node_id)
        if not any(state != "pending" for state in self.nodes.values()) and not rerun:
            return self.begin()
        return rerun


class WaitingRun(BaseModel):
    """waiting_input으로 멈춘 노드 런. 재개하면 같은 node_run_id로 이어 간다."""

    node_run_id: uuid.UUID
    index: int
    attempt: int
    child_run_id: uuid.UUID | None = None
    # 재개 때 node.started에 함께 싣는다(3.8). 화면이 자식 세션 스트림에 바로 붙는다.
    child_session_id: uuid.UUID | None = None


class IterationProgress(BaseModel):
    """입력 대기로 멈춘 iteration 노드의 NodeResult.state(3.5, 3.7).

    끝난 반복의 출력은 풀의 "<노드>@<index>"에 이미 있으므로 상태만 남긴다. 멈춘 반복은 재개 때
    체크포인트의 @index 노드·엣지 상태로 이어 간다.
    """

    done: dict[str, Literal["succeeded", "failed"]] = Field(
        default_factory=dict[str, Literal["succeeded", "failed"]]
    )
    stalled: list[int] = Field(default_factory=list[int])
    # 앞 구간들에서 반복을 돈 시간. iteration.completed의 duration_ms에 더한다.
    elapsed_ms: int = 0


class Checkpoint(BaseModel):
    """wf_run.state에 쓰는 런 상태(3.7). waiting_input 전환 직전과 런 실패 때 쓴다.

    pool은 마스킹을 거친 "<scope>.<name>" → 값이고 env는 넣지 않는다(재개 때 버전에서 다시 푼다).
    상태 키는 iteration 안이면 "@<index>"가 붙는다.
    """

    version: Literal[1] = 1
    pool: dict[str, Any] = Field(default_factory=dict[str, Any])
    skipped: list[str] = Field(default_factory=list[str])
    empty: list[str] = Field(default_factory=list[str])
    node_states: dict[str, str] = Field(default_factory=dict[str, str])
    edge_states: dict[str, str] = Field(default_factory=dict[str, str])
    # NodeResult.state. 재개 때 그 노드의 run()에 state로 돌아간다.
    node_state: dict[str, dict[str, Any]] = Field(default_factory=dict[str, dict[str, Any]])
    waiting: dict[str, WaitingRun] = Field(default_factory=dict[str, WaitingRun])
    # 체크포인트 때 살아 있던 반복 프레임 [{iteration_id, index}]. 그 상태는 node_states·
    # edge_states의 @index 키에 있고, 어느 반복이 끝났는지는 iteration 노드의 node_state에 있다.
    frames: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    outputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    answer_text: str = ""
    conv_vars: dict[str, Any] = Field(default_factory=dict[str, Any])
    usage: UsageTotal = Field(default_factory=UsageTotal)
    # 자식 런을 포함한 토큰 합. max_total_tokens 판정에 쓴다.
    budget_tokens: int = 0
    next_index: int = 0
    node_runs: int = 0
    elapsed_ms: int = 0


def inner_nodes(frames: Iterable[FrameGraph]) -> dict[str, frozenset[str]]:
    """iteration id → 그 하위 그래프 노드 id."""
    return {
        frame.iteration_id: frozenset(frame.nodes)
        for frame in frames
        if frame.iteration_id is not None
    }
