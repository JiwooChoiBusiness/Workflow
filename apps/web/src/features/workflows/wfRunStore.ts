/*
 * 워크플로우 런 하나의 화면 상태(설계 7.3 wfRunStore). 3.9의 이벤트(workflow.*, node.*, iteration.*,
 * answer.*)를 리듀서로 쌓는다. 편집기 실행 패널·캔버스 카드·로그 화면·웹앱이 같은 상태를 쓰고, 연결과
 * 재접속은 wfRunController가 한다.
 */
import { createStore, type StoreApi } from "zustand/vanilla";
import type { RunEventDataMap } from "@/api/run-events.gen";
import type { WaitingInputData, WfEvent, WfEventOf, WfEventType } from "@/api/run-events";
import type { WfNodeRunOut, WfRunOut } from "@/api/types";
import { emptyUsage, usageFromTotal, type UsageTotals } from "@/stores/runStore";

// llm 노드의 델타는 카드 미리보기로만 쓴다. 전문은 노드 런 상세(GET .../node-runs/{nid})에 있다.
export const NODE_TEXT_LIMIT = 64 * 1024;

export type WfRunStatus =
  "idle" | "queued" | "running" | "waiting_input" | "succeeded" | "failed" | "cancelled";

export type NodeRunStatus = RunEventDataMap["node.completed"]["status"] | "running";

export interface WfRunError {
  code: string;
  message: string;
  nodeId: string | null;
  retryable: boolean;
}

export interface NodeRunError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface NodeRunView {
  id: string;
  nodeId: string;
  // 건너뛴 노드는 node.started 없이 node.completed만 와서 타입·이름·순번을 모른다. 화면이 그래프에서 찾는다.
  nodeType: string | null;
  title: string | null;
  index: number | null;
  iterationId: string | null;
  iterationIndex: number | null;
  attempt: number;
  status: NodeRunStatus;
  startedAt: string | null;
  endedAt: string | null;
  durationMs: number | null;
  outputsPreview: Record<string, unknown> | null;
  truncated: boolean;
  error: NodeRunError | null;
  usage: UsageTotals | null;
  branch: string | null;
  warnings: string[];
  text: string;
  textDropped: boolean;
  // agent 노드의 자식 런(1단계 run)과 그 과업 세션
  childRunId: string | null;
  childSessionId: string | null;
  steps: number | null;
  // 자식 런이 에이전트 레인 대기열에 있으면 그 순번, 레이트 리밋으로 기다리면 reason만
  waiting: { reason: "queue" | "rate_limit"; position: number | null } | null;
  // 이 시도가 실패해 같은 노드를 다시 시도했다(node.retrying)
  retried: boolean;
}

export interface IterationView {
  nodeId: string;
  total: number | null;
  // 마지막으로 시작한 반복의 순번(iteration.next)
  index: number | null;
  count: number | null;
  failedCount: number | null;
  durationMs: number | null;
}

export interface WfPendingInteraction {
  nodeId: string;
  iterationIndex: number | null;
  childRunId: string | null;
  interaction: WaitingInputData;
}

interface Frame {
  iterationId: string | null;
  iterationIndex: number | null;
}

export interface WfRunViewState {
  runId: string | null;
  status: WfRunStatus;
  queuePosition: number | null;
  startedAt: string | null;
  endedAt: string | null;
  resumed: boolean;
  nodeCount: number | null;
  inputsPreview: Record<string, unknown> | null;
  nodeRuns: Record<string, NodeRunView>;
  // 노드 런이 처음 보인 순서(모든 프레임)
  order: string[];
  // 노드 id → 가장 최근 노드 런 id. 캔버스 카드가 자기 상태를 이것으로 찾는다.
  latestByNode: Record<string, string>;
  iterations: Record<string, IterationView>;
  outputs: Record<string, unknown> | null;
  outputsTruncated: boolean;
  answerText: string;
  pendingInteraction: WfPendingInteraction | null;
  usage: UsageTotals;
  error: WfRunError | null;
  durationMs: number | null;
  lastSeq: number;
  // 마지막으로 끝난 노드 런의 프레임. 시작 없이 오는 건너뛴 노드 런을 같은 프레임에 둔다(건너뜀은 앞 노드가
  // 끝나며 같은 프레임 안에서 정해진다).
  lastFrame: Frame;
}

export function initialWfRunState(partial: Partial<WfRunViewState> = {}): WfRunViewState {
  return {
    runId: null,
    status: "idle",
    queuePosition: null,
    startedAt: null,
    endedAt: null,
    resumed: false,
    nodeCount: null,
    inputsPreview: null,
    nodeRuns: {},
    order: [],
    latestByNode: {},
    iterations: {},
    outputs: null,
    outputsTruncated: false,
    answerText: "",
    pendingInteraction: null,
    usage: emptyUsage(),
    error: null,
    durationMs: null,
    lastSeq: 0,
    lastFrame: { iterationId: null, iterationIndex: null },
    ...partial,
  };
}

export function isWfRunActive(status: WfRunStatus): boolean {
  return status === "queued" || status === "running";
}

export function isWfRunOpen(status: WfRunStatus): boolean {
  return isWfRunActive(status) || status === "waiting_input";
}

export function isNodeRunFinished(status: NodeRunStatus): boolean {
  return status !== "running" && status !== "waiting_input";
}

function addUsage(total: UsageTotals, add: UsageTotals): UsageTotals {
  return {
    inputTokens: total.inputTokens + add.inputTokens,
    outputTokens: total.outputTokens + add.outputTokens,
    reasoningTokens: total.reasoningTokens + add.reasoningTokens,
    cachedInputTokens: total.cachedInputTokens + add.cachedInputTokens,
    costUsd: add.costUsd === null ? total.costUsd : (total.costUsd ?? 0) + add.costUsd,
    estimated: total.estimated || add.estimated,
  };
}

function emptyNodeRun(id: string, nodeId: string, frame: Frame): NodeRunView {
  return {
    id,
    nodeId,
    nodeType: null,
    title: null,
    index: null,
    iterationId: frame.iterationId,
    iterationIndex: frame.iterationIndex,
    attempt: 1,
    status: "running",
    startedAt: null,
    endedAt: null,
    durationMs: null,
    outputsPreview: null,
    truncated: false,
    error: null,
    usage: null,
    branch: null,
    warnings: [],
    text: "",
    textDropped: false,
    childRunId: null,
    childSessionId: null,
    steps: null,
    waiting: null,
    retried: false,
  };
}

const ROOT: Frame = { iterationId: null, iterationIndex: null };

// 노드 런을 고치거나(없으면 frame에 새로 만든다) 순서·최신 표를 함께 맞춘다.
function withNodeRun(
  state: WfRunViewState,
  id: string,
  nodeId: string,
  frame: Frame,
  update: (run: NodeRunView) => NodeRunView,
): WfRunViewState {
  const current = state.nodeRuns[id];
  const next = update(current ?? emptyNodeRun(id, nodeId, frame));
  return {
    ...state,
    nodeRuns: { ...state.nodeRuns, [id]: next },
    order: current ? state.order : [...state.order, id],
    latestByNode:
      state.latestByNode[nodeId] === id
        ? state.latestByNode
        : { ...state.latestByNode, [nodeId]: id },
  };
}

// node_run_id가 없는 이벤트(node.waiting)는 그 노드의 가장 최근 노드 런에 붙인다.
function nodeRunIdOf(state: WfRunViewState, nodeId: string, nodeRunId?: string | null) {
  return nodeRunId ?? state.latestByNode[nodeId] ?? null;
}

function nodeError(error: RunEventDataMap["node.completed"]["error"]): NodeRunError | null {
  return error
    ? { code: error.code, message: error.message, retryable: error.retryable ?? false }
    : null;
}

function runError(error: RunEventDataMap["workflow.error"] | null | undefined): WfRunError | null {
  return error
    ? {
        code: error.code,
        message: error.message,
        nodeId: error.node_id ?? null,
        retryable: error.retryable ?? false,
      }
    : null;
}

type Handler<T extends WfEventType> = (
  state: WfRunViewState,
  event: WfEventOf<T>,
) => WfRunViewState;

const handlers: { [T in WfEventType]?: Handler<T> } = {
  "workflow.queued": (state, event) => ({
    ...state,
    status: "queued",
    queuePosition: event.data.position,
    // 다시 큐에 들어왔으면 기다리던 승인·질문은 답을 받은 것이다.
    pendingInteraction: null,
    endedAt: null,
  }),

  "workflow.started": (state, event) => ({
    ...state,
    status: "running",
    queuePosition: null,
    resumed: event.data.resumed,
    startedAt: event.data.resumed && state.startedAt ? state.startedAt : event.ts,
    nodeCount: event.data.node_count,
    inputsPreview: event.data.inputs_preview,
    pendingInteraction: null,
  }),

  "workflow.resumed": (state) => ({ ...state, pendingInteraction: null }),

  "workflow.waiting_input": (state, event) => ({
    ...state,
    pendingInteraction: {
      nodeId: event.data.node_id,
      iterationIndex: event.data.iteration_index ?? null,
      childRunId: event.data.child_run_id ?? null,
      interaction: event.data.interaction,
    },
  }),

  "workflow.error": (state, event) => ({ ...state, error: runError(event.data) }),

  "workflow.completed": (state, event) => {
    const data = event.data;
    const waiting = data.status === "waiting_input";
    return {
      ...state,
      status: data.status,
      queuePosition: null,
      outputs: data.outputs ?? null,
      outputsTruncated: data.truncated ?? false,
      usage: usageFromTotal(data.usage_total),
      durationMs: data.duration_ms,
      endedAt: waiting ? null : event.ts,
      error: runError(data.error) ?? (data.status === "failed" ? state.error : null),
      pendingInteraction: waiting ? state.pendingInteraction : null,
    };
  },

  "node.started": (state, event) => {
    const data = event.data;
    const frame: Frame = {
      iterationId: data.iteration_id ?? null,
      iterationIndex: data.iteration_index ?? null,
    };
    return withNodeRun(state, data.node_run_id, data.node_id, frame, (run) => ({
      ...run,
      nodeType: data.node_type,
      title: data.title,
      index: data.index,
      iterationId: frame.iterationId,
      iterationIndex: frame.iterationIndex,
      attempt: data.attempt,
      status: "running",
      // 재개된 노드 런은 같은 id로 다시 시작한다. 처음 시작한 시각을 둔다.
      startedAt: run.startedAt ?? event.ts,
      endedAt: null,
      childRunId: data.child_run_id ?? run.childRunId,
      childSessionId: data.child_session_id ?? run.childSessionId,
      waiting: null,
    }));
  },

  "node.text_delta": (state, event) => {
    const data = event.data;
    const id = nodeRunIdOf(state, data.node_id, data.node_run_id);
    if (!id) return state;
    return withNodeRun(state, id, data.node_id, ROOT, (run) => {
      if (run.text.length >= NODE_TEXT_LIMIT) return { ...run, textDropped: true };
      const text = run.text + data.text;
      return text.length > NODE_TEXT_LIMIT
        ? { ...run, text: text.slice(0, NODE_TEXT_LIMIT), textDropped: true }
        : { ...run, text };
    });
  },

  "node.progress": (state, event) => {
    const data = event.data;
    return withNodeRun(state, data.node_run_id, data.node_id, ROOT, (run) => ({
      ...run,
      steps: data.steps ?? run.steps,
      usage: data.usage ? usageFromTotal(data.usage) : run.usage,
      childRunId: data.child_run_id ?? run.childRunId,
      childSessionId: data.child_session_id ?? run.childSessionId,
      waiting:
        data.position !== null && data.position !== undefined
          ? { reason: "queue", position: data.position }
          : null,
    }));
  },

  "node.waiting": (state, event) => {
    const data = event.data;
    const id = nodeRunIdOf(state, data.node_id, data.node_run_id);
    if (!id) return state;
    return withNodeRun(state, id, data.node_id, ROOT, (run) => ({
      ...run,
      waiting: { reason: data.reason, position: data.position ?? null },
    }));
  },

  "node.retrying": (state, event) => {
    const data = event.data;
    return withNodeRun(state, data.node_run_id, data.node_id, ROOT, (run) => ({
      ...run,
      retried: true,
    }));
  },

  // 모델 호출 한 번의 사용량. 런 합계는 workflow.completed의 usage_total이 바로잡는다.
  "node.usage": (state, event) => {
    const data = event.data;
    const add: UsageTotals = {
      inputTokens: data.input_tokens,
      outputTokens: data.output_tokens,
      reasoningTokens: data.reasoning_tokens ?? 0,
      cachedInputTokens: data.cached_input_tokens ?? 0,
      costUsd: data.cost_usd ?? null,
      estimated: data.estimated,
    };
    const next = withNodeRun(state, data.node_run_id, data.node_id, ROOT, (run) => ({
      ...run,
      usage: addUsage(run.usage ?? emptyUsage(), add),
    }));
    return { ...next, usage: addUsage(state.usage, add) };
  },

  "node.completed": (state, event) => {
    const data = event.data;
    const known = state.nodeRuns[data.node_run_id];
    const frame = known
      ? { iterationId: known.iterationId, iterationIndex: known.iterationIndex }
      : state.lastFrame;
    const next = withNodeRun(state, data.node_run_id, data.node_id, frame, (run) => ({
      ...run,
      status: data.status,
      outputsPreview: data.outputs_preview ?? null,
      truncated: data.truncated ?? false,
      error: nodeError(data.error),
      usage: data.usage ? usageFromTotal(data.usage) : run.usage,
      durationMs: data.duration_ms,
      endedAt: data.status === "waiting_input" ? null : event.ts,
      branch: data.branch ?? null,
      warnings: data.warnings ?? [],
      waiting: null,
    }));
    return { ...next, lastFrame: frame };
  },

  "iteration.started": (state, event) => {
    const data = event.data;
    const current = state.iterations[data.node_id];
    return {
      ...state,
      iterations: {
        ...state.iterations,
        [data.node_id]: {
          nodeId: data.node_id,
          total: data.total,
          index: current?.index ?? null,
          count: null,
          failedCount: null,
          durationMs: null,
        },
      },
    };
  },

  "iteration.next": (state, event) => {
    const data = event.data;
    const current = state.iterations[data.node_id];
    return {
      ...state,
      iterations: {
        ...state.iterations,
        [data.node_id]: {
          nodeId: data.node_id,
          total: current?.total ?? null,
          index: data.index,
          count: current?.count ?? null,
          failedCount: current?.failedCount ?? null,
          durationMs: current?.durationMs ?? null,
        },
      },
    };
  },

  "iteration.completed": (state, event) => {
    const data = event.data;
    const current = state.iterations[data.node_id];
    return {
      ...state,
      iterations: {
        ...state.iterations,
        [data.node_id]: {
          nodeId: data.node_id,
          total: current?.total ?? data.count,
          index: current?.index ?? null,
          count: data.count,
          failedCount: data.failed_count,
          durationMs: data.duration_ms,
        },
      },
    };
  },

  "answer.delta": (state, event) => ({ ...state, answerText: state.answerText + event.data.text }),

  "answer.completed": (state, event) => ({ ...state, answerText: event.data.text }),
};

export type WfStreamEvent = WfEvent | { type: "ping" };

export function applyWfEvent(state: WfRunViewState, event: WfStreamEvent): WfRunViewState {
  if (event.type === "ping") return state;
  const handler = handlers[event.type] as Handler<WfEventType> | undefined;
  if (!handler) return state;
  let base = state;
  if (state.runId !== event.run_id) {
    // 다른 런의 이벤트가 오면 그 런으로 새로 시작한다(같은 스토어로 다음 런을 돌릴 때).
    base = initialWfRunState({ runId: event.run_id });
  } else if (event.seq > 0 && event.seq <= state.lastSeq) {
    // 재접속하며 다시 받은 이벤트
    return state;
  }
  const next = handler(base, event as WfEventOf<WfEventType>);
  return { ...next, lastSeq: Math.max(base.lastSeq, event.seq) };
}

export function applyWfEvents(
  state: WfRunViewState,
  events: readonly WfStreamEvent[],
): WfRunViewState {
  return events.reduce(applyWfEvent, state);
}

// 이벤트가 보존 기간으로 지워진 런은 런 상세(WfRunDetail)의 요약으로 그린다. iteration 안쪽은 따로 받는다.
export function stateFromRun(run: WfRunOut, nodeRuns: readonly WfNodeRunOut[]): WfRunViewState {
  let state = initialWfRunState({
    runId: run.id,
    status: run.status,
    startedAt: run.started_at,
    endedAt: run.ended_at,
    outputs: run.outputs,
    usage: usageFromTotal(run.usage),
    error: run.error
      ? {
          code: run.error.code,
          message: run.error.message,
          nodeId: run.error.node_id ?? null,
          retryable: run.error.retryable ?? false,
        }
      : null,
    pendingInteraction: run.pending_interaction
      ? {
          nodeId: run.pending_interaction.node_id,
          iterationIndex: run.pending_interaction.iteration_index ?? null,
          childRunId: run.pending_interaction.child_run_id ?? null,
          interaction: run.pending_interaction.interaction,
        }
      : null,
  });
  state = mergeNodeRuns(state, nodeRuns);
  return state;
}

/*
 * 서버의 노드 런 목록(GET .../node-runs, 런 상세의 node_runs)을 넣는다. 이미 이벤트로 받은 런은 그쪽 값을
 * 두고 자식 런 id만 채운다. 저장된 이벤트에는 자식 런 id가 재개 때의 node.started에만 있다(만든 직후의
 * node.progress는 저장하지 않는다).
 */
export function mergeNodeRuns(
  state: WfRunViewState,
  nodeRuns: readonly WfNodeRunOut[],
): WfRunViewState {
  let next = state;
  for (const item of [...nodeRuns].sort((a, b) => a.index - b.index || a.attempt - b.attempt)) {
    const existing = next.nodeRuns[item.id];
    if (existing) {
      if (!existing.childRunId && item.child_run_id) {
        next = {
          ...next,
          nodeRuns: { ...next.nodeRuns, [item.id]: { ...existing, childRunId: item.child_run_id } },
        };
      }
      continue;
    }
    const frame: Frame = {
      iterationId: item.iteration_node_id ?? null,
      iterationIndex: item.iteration_index ?? null,
    };
    next = withNodeRun(next, item.id, item.node_id, frame, (run) => ({
      ...run,
      nodeType: item.node_type,
      title: item.title,
      index: item.index,
      attempt: item.attempt,
      status: item.status,
      startedAt: item.started_at,
      endedAt: item.ended_at ?? null,
      durationMs: item.duration_ms ?? null,
      truncated: item.truncated,
      error: item.error
        ? {
            code: item.error.code,
            message: item.error.message,
            retryable: item.error.retryable ?? false,
          }
        : null,
      usage: item.usage ? usageFromTotal(item.usage) : null,
      branch: item.branch ?? null,
      warnings: item.warnings,
      childRunId: item.child_run_id ?? null,
    }));
  }
  return next;
}

// 끝난 노드 런 수. 상태 표시줄의 "노드 n개"가 쓴다(건너뜀 제외).
export function finishedNodeCount(state: WfRunViewState): number {
  let count = 0;
  for (const run of Object.values(state.nodeRuns)) {
    if (run.status !== "running" && run.status !== "skipped" && run.status !== "waiting_input") {
      count += 1;
    }
  }
  return count;
}

// 상태 표시줄에 넣을 지금 도는 노드의 이름. 여러 개면 먼저 시작한 것이다.
export function runningNode(state: WfRunViewState): string | null {
  for (const id of state.order) {
    const run = state.nodeRuns[id];
    if (run?.status === "running" && run.nodeType !== "iteration") return run.title ?? run.nodeId;
  }
  return null;
}

const DELTA_TYPES = new Set<string>(["node.text_delta", "answer.delta"]);

export interface WfRunStoreActions {
  // 델타는 다음 애니메이션 프레임에 모아서, 그 밖의 이벤트는 쌓인 것과 함께 바로 반영한다.
  applyEvent: (event: WfStreamEvent) => void;
  applyEvents: (events: readonly WfStreamEvent[]) => void;
  flush: () => void;
  reset: (partial?: Partial<WfRunViewState>) => void;
  load: (state: WfRunViewState) => void;
  mergeNodeRuns: (nodeRuns: readonly WfNodeRunOut[]) => void;
}

export type WfRunStoreState = WfRunViewState & { actions: WfRunStoreActions };
export type WfRunStore = StoreApi<WfRunStoreState>;

type Schedule = (callback: () => void) => () => void;

const frameSchedule: Schedule = (callback) => {
  if (typeof requestAnimationFrame === "function") {
    const handle = requestAnimationFrame(() => callback());
    return () => cancelAnimationFrame(handle);
  }
  const handle = setTimeout(callback, 16);
  return () => clearTimeout(handle);
};

export function createWfRunStore(
  options: { initial?: Partial<WfRunViewState>; schedule?: Schedule } = {},
): WfRunStore {
  const schedule = options.schedule ?? frameSchedule;
  let queue: WfStreamEvent[] = [];
  let cancelScheduled: (() => void) | null = null;

  return createStore<WfRunStoreState>()((set) => {
    const flush = () => {
      cancelScheduled?.();
      cancelScheduled = null;
      if (queue.length === 0) return;
      const events = queue;
      queue = [];
      set((state) => applyWfEvents(state, events));
    };

    const actions: WfRunStoreActions = {
      applyEvent(event) {
        queue.push(event);
        if (!DELTA_TYPES.has(event.type)) {
          flush();
        } else if (!cancelScheduled) {
          cancelScheduled = schedule(flush);
        }
      },
      applyEvents(events) {
        queue.push(...events);
        flush();
      },
      flush,
      reset(partial) {
        cancelScheduled?.();
        cancelScheduled = null;
        queue = [];
        set({ ...initialWfRunState(partial) });
      },
      load(state) {
        cancelScheduled?.();
        cancelScheduled = null;
        queue = [];
        set({ ...state });
      },
      mergeNodeRuns(nodeRuns) {
        flush();
        set((state) => mergeNodeRuns(state, nodeRuns));
      },
    };

    return { ...initialWfRunState(options.initial), actions };
  });
}
