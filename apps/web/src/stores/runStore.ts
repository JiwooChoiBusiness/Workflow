import { createStore, type StoreApi } from "zustand/vanilla";
import type {
  InteractionKind,
  MessageFileData,
  RunEvent,
  RunEventOf,
  RunEventType,
  ToolCallResult,
  UsageTotal,
  WaitingInputData,
} from "@/api/run-events";
import type { ApprovalMode } from "@/api/types";

// 도구 출력은 화면에 이만큼(문자 수)만 들고 있고, 넘치면 앞부분을 버린다. 전체는 서버에서 내려받는다(설계 7.4).
export const TOOL_OUTPUT_LIMIT = 200 * 1024;
export const CALL_AGENT = "call_agent";

export type RunStatus =
  "idle" | "queued" | "running" | "waiting_input" | "succeeded" | "failed" | "cancelled";

export interface UsageTotals {
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  cachedInputTokens: number;
  costUsd: number | null;
  estimated: boolean;
}

export interface OutputChunk {
  stream: "stdout" | "stderr";
  text: string;
}

// child: call_agent가 만든 하위 에이전트 런이 끝나기를 기다린다(오케스트레이션 8.1).
export type ToolPhase = "input" | "ready" | "awaiting_approval" | "running" | "child" | "done";

export interface ChildCallEnd {
  status: "succeeded" | "failed" | "cancelled";
  error: RunError | null;
  steps: number;
  usage: UsageTotals;
  durationMs: number;
  // 하위 에이전트가 남긴 결과 파일 수
  files: number;
}

// call_agent 호출 하나가 만든 하위 에이전트 런. child_run.started가 채우고 child_run.completed가 end를 적는다.
export interface ChildCallView {
  runId: string;
  sessionId: string;
  agentId: string;
  agentSlug: string;
  agentName: string;
  // 미리보기에서 발행 버전이 없는 하위 에이전트는 초안으로 돈다(설계 2.4).
  version: "draft" | "published";
  depth: number;
  resultFormat: "text" | "json";
  taskPreview: string;
  end: ChildCallEnd | null;
}

// 새로 고친 직후 세션 상세의 active_run.child로 아는 하위 호출. 재생된 child_run.started가 도구 카드를
// 채우기 전까지 상태 표시줄이 쓴다.
export interface ActiveChildHint {
  toolCallId: string;
  runId: string;
  sessionId: string;
  agentSlug: string;
  agentName: string;
}

export interface ToolCallView {
  id: string;
  runId: string;
  step: number;
  name: string;
  phase: ToolPhase;
  result: ToolCallResult | null;
  inputText: string;
  input: Record<string, unknown> | null;
  output: OutputChunk[];
  outputChars: number;
  droppedChars: number;
  // 화면의 출력이 tool_call.completed의 발췌이고 실제 출력은 더 길다(전체는 내려받기로).
  outputIsExcerpt: boolean;
  // 메시지 기록(tool_result 블록)에서 다시 그린 카드. 출력은 모델에 넘긴 내용이고 종료 코드·소요 시간이 없다.
  restored: boolean;
  exitCode: number | null;
  timedOut: boolean;
  durationMs: number | null;
  peakMemoryBytes: number | null;
  outputBytes: number | null;
  outputStorageKey: string | null;
  // call_agent 호출이면 그 하위 에이전트 런(오케스트레이션 6.1). 메시지 기록에서 다시 그린 카드는 비어 있다.
  child: ChildCallView | null;
}

export interface ChangedFile {
  path: string;
  size: number;
  sessionFileId: string | null;
  downloadable: boolean;
  reason: string | null;
  runId: string;
}

// 사용자 메시지에 붙인 파일(session_file kind=upload)
export interface AttachmentView {
  sessionFileId: string | null;
  name: string;
  size: number;
  downloadable: boolean;
}

export interface PendingInteraction extends WaitingInputData {
  runId: string;
}

export type NoticeCode =
  | "retrying"
  | "workspace_restoring"
  | "workspace_restored"
  | "workspace_restore_warning"
  | "workspace_recreated"
  | "prompt_changed"
  | "step_limit"
  | "changes_truncated"
  | "stopped"
  | "input_timeout"
  | "history_truncated";

export type TimelineItem =
  | { kind: "user"; id: string; text: string; files?: AttachmentView[] }
  | {
      kind: "text";
      id: string;
      runId: string;
      messageId: string;
      step: number;
      text: string;
      done: boolean;
    }
  | { kind: "thinking"; id: string; runId: string; step: number; text: string }
  | { kind: "tool"; id: string; toolCallId: string }
  // 메시지 기록의 요약(system_summary)에는 토큰 수가 없다.
  | { kind: "compaction"; id: string; beforeTokens: number | null; afterTokens: number | null }
  | {
      kind: "interaction";
      id: string;
      interactionKind: InteractionKind;
      decision: "approve" | "reject" | null;
      note: string | null;
      answer: string | null;
      autoApprove: boolean;
    }
  | { kind: "notice"; id: string; code: NoticeCode; message: string | null; count: number | null }
  | { kind: "error"; id: string; code: string; message: string; retryable: boolean };

export interface RunError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface RunViewState {
  sessionId: string | null;
  runId: string | null;
  status: RunStatus;
  items: TimelineItem[];
  toolCalls: Record<string, ToolCallView>;
  files: ChangedFile[];
  pendingInteraction: PendingInteraction | null;
  usage: UsageTotals;
  step: number;
  stepLimit: number | null;
  model: string | null;
  approvalMode: ApprovalMode | null;
  queuePosition: number | null;
  startedAt: string | null;
  endedAt: string | null;
  // 런별 마지막 seq. 재접속하면서 같은 이벤트가 다시 오면 버린다.
  lastSeq: Record<string, number>;
  error: RunError | null;
  sessionAutoApprove: boolean;
  activeChild: ActiveChildHint | null;
}

export function emptyUsage(): UsageTotals {
  return {
    inputTokens: 0,
    outputTokens: 0,
    reasoningTokens: 0,
    cachedInputTokens: 0,
    costUsd: null,
    estimated: false,
  };
}

export function initialRunState(partial: Partial<RunViewState> = {}): RunViewState {
  return {
    sessionId: null,
    runId: null,
    status: "idle",
    items: [],
    toolCalls: {},
    files: [],
    pendingInteraction: null,
    usage: emptyUsage(),
    step: 0,
    stepLimit: null,
    model: null,
    approvalMode: null,
    queuePosition: null,
    startedAt: null,
    endedAt: null,
    lastSeq: {},
    error: null,
    sessionAutoApprove: false,
    activeChild: null,
    ...partial,
  };
}

export function isRunActive(status: RunStatus): boolean {
  return status === "queued" || status === "running";
}

// 아직 끝나지 않은 런. 입력을 기다리는 런도 중지할 수 있다.
export function isRunOpen(status: RunStatus): boolean {
  return isRunActive(status) || status === "waiting_input";
}

// 지금 런이 끝나기를 기다리는 하위 에이전트의 이름(없으면 null).
export function waitingChildName(state: RunViewState): string | null {
  if (!isRunOpen(state.status)) return null;
  for (const tool of Object.values(state.toolCalls)) {
    const child = tool.child;
    if (tool.phase === "child" && child && !child.end && tool.runId === state.runId) {
      return child.agentName || child.agentSlug;
    }
  }
  const hint = state.activeChild;
  return hint ? hint.agentName || hint.agentSlug : null;
}

export function usageFromTotal(total: UsageTotal): UsageTotals {
  return {
    inputTokens: total.input_tokens ?? 0,
    outputTokens: total.output_tokens ?? 0,
    reasoningTokens: total.reasoning_tokens ?? 0,
    cachedInputTokens: total.cached_input_tokens ?? 0,
    costUsd: total.cost_usd ?? null,
    estimated: Boolean(total.estimated),
  };
}

const encoder = new TextEncoder();

function utf8Length(text: string): number {
  return encoder.encode(text).length;
}

export function newToolCall(id: string, runId: string, step: number, name: string): ToolCallView {
  return {
    id,
    runId,
    step,
    name,
    phase: "input",
    result: null,
    inputText: "",
    input: null,
    output: [],
    outputChars: 0,
    droppedChars: 0,
    outputIsExcerpt: false,
    restored: false,
    exitCode: null,
    timedOut: false,
    durationMs: null,
    peakMemoryBytes: null,
    outputBytes: null,
    outputStorageKey: null,
    child: null,
  };
}

// 같은 스트림이 이어지면 마지막 조각에 붙이고, 상한을 넘으면 앞에서부터 잘라 낸다.
export function appendOutput(
  tool: ToolCallView,
  stream: OutputChunk["stream"],
  text: string,
  limit: number = TOOL_OUTPUT_LIMIT,
): ToolCallView {
  if (!text) return tool;
  const output = tool.output.slice();
  const last = output[output.length - 1];
  if (last && last.stream === stream) {
    output[output.length - 1] = { stream, text: last.text + text };
  } else {
    output.push({ stream, text });
  }
  let chars = tool.outputChars + text.length;
  let dropped = tool.droppedChars;
  while (chars > limit && output.length > 0) {
    const excess = chars - limit;
    const first = output[0];
    if (first.text.length <= excess) {
      output.shift();
      chars -= first.text.length;
      dropped += first.text.length;
    } else {
      output[0] = { stream: first.stream, text: first.text.slice(excess) };
      chars -= excess;
      dropped += excess;
    }
  }
  return { ...tool, output, outputChars: chars, droppedChars: dropped };
}

// 같은 경로는 나중 것이 이긴다. file.changed와 message.completed.files가 같은 파일을 두 번 알린다.
export function mergeFiles(files: ChangedFile[], incoming: ChangedFile[]): ChangedFile[] {
  if (incoming.length === 0) return files;
  const paths = new Set(incoming.map((file) => file.path));
  return [...files.filter((file) => !paths.has(file.path)), ...incoming];
}

export function changedFileOf(file: MessageFileData, runId: string): ChangedFile {
  return {
    path: file.path,
    size: file.size,
    sessionFileId: file.session_file_id ?? null,
    downloadable: file.downloadable,
    reason: null,
    runId,
  };
}

function withTool<S extends RunViewState>(
  state: S,
  id: string,
  update: (tool: ToolCallView) => ToolCallView,
  create?: () => ToolCallView,
): S {
  const existing = state.toolCalls[id];
  if (!existing && !create) return state;
  const base = existing ?? create!();
  const items = existing ? state.items : [...state.items, toolItem(id)];
  return { ...state, items, toolCalls: { ...state.toolCalls, [id]: update(base) } };
}

function toolItem(toolCallId: string): TimelineItem {
  return { kind: "tool", id: `tool:${toolCallId}`, toolCallId };
}

function pushItem<S extends RunViewState>(state: S, item: TimelineItem): S {
  return { ...state, items: [...state.items, item] };
}

function markTextDone(items: TimelineItem[], runId: string): TimelineItem[] {
  let changed = false;
  const next = items.map((item) => {
    if (item.kind === "text" && item.runId === runId && !item.done) {
      changed = true;
      return { ...item, done: true };
    }
    return item;
  });
  return changed ? next : items;
}

// 런이 끝났는데 결과가 없는 도구(중지·실패로 실행하지 못한 호출)는 취소됨으로 닫는다.
function closeOpenTools(
  toolCalls: Record<string, ToolCallView>,
  runId: string,
): Record<string, ToolCallView> {
  let next: Record<string, ToolCallView> | null = null;
  for (const tool of Object.values(toolCalls)) {
    if (tool.runId !== runId || tool.phase === "done") continue;
    next ??= { ...toolCalls };
    next[tool.id] = { ...tool, phase: "done", result: "cancelled" };
  }
  return next ?? toolCalls;
}

function startRun<S extends RunViewState>(state: S, runId: string, sessionId: string): S {
  return {
    ...state,
    runId,
    sessionId: sessionId || state.sessionId,
    status: "running",
    usage: emptyUsage(),
    step: 0,
    stepLimit: null,
    model: null,
    queuePosition: null,
    startedAt: null,
    endedAt: null,
    error: null,
    // 새 런이 시작되면 이전 런의 질문은 새 메시지로 답한 것이다(설계 4.9).
    pendingInteraction: null,
    activeChild: null,
  };
}

type Handler<T extends RunEventType> = <S extends RunViewState>(
  state: S,
  event: RunEventOf<T>,
) => S;
type HandlerMap = { [T in RunEventType]: Handler<T> };

function noticeId(state: RunViewState, event: { run_id: string; seq: number }, code: string) {
  return `notice:${event.run_id}:${event.seq}:${code}:${state.items.length}`;
}

type WorkspaceEventType =
  | "workspace.restoring"
  | "workspace.restored"
  | "workspace.restore_warning"
  | "workspace.recreated";

function workspaceNotice<E extends WorkspaceEventType>(code: NoticeCode): Handler<E> {
  return (state, event) =>
    pushItem(state, {
      kind: "notice",
      id: noticeId(state, event, code),
      code,
      message: event.data.message ?? null,
      count: event.data.packages ?? null,
    });
}

const handlers: HandlerMap = {
  "run.queued": (state, event) => ({
    ...state,
    status: "queued",
    queuePosition: event.data.position,
  }),

  "run.started": (state, event) => ({
    ...state,
    status: "running",
    queuePosition: null,
    stepLimit: event.data.step_limit,
    // run.started의 model은 처음 시도할 모델이다. 재개된 런은 usage로 받은 실제 모델을 그대로 둔다.
    model: state.model ?? event.data.model,
    approvalMode: event.data.approval_mode,
    startedAt: state.startedAt ?? event.ts,
  }),

  "workspace.restoring": workspaceNotice("workspace_restoring"),
  "workspace.restored": workspaceNotice("workspace_restored"),
  "workspace.restore_warning": workspaceNotice("workspace_restore_warning"),
  "workspace.recreated": workspaceNotice("workspace_recreated"),

  "step.started": (state, event) => ({ ...state, step: event.data.step }),

  "message.delta": (state, event) => {
    const { message_id: messageId, text } = event.data;
    const items = state.items;
    const last = items[items.length - 1];
    const status = state.status === "queued" ? "running" : state.status;
    if (
      last &&
      last.kind === "text" &&
      last.messageId === messageId &&
      last.runId === event.run_id &&
      !last.done
    ) {
      const updated = { ...last, text: last.text + text };
      return { ...state, status, items: [...items.slice(0, -1), updated] };
    }
    const segment = items.filter((i) => i.kind === "text" && i.messageId === messageId).length;
    return {
      ...state,
      status,
      items: [
        ...items,
        {
          kind: "text",
          id: `text:${messageId}:${segment}`,
          runId: event.run_id,
          messageId,
          step: state.step,
          text,
          done: false,
        },
      ],
    };
  },

  "thinking.delta": (state, event) => {
    const items = state.items;
    const last = items[items.length - 1];
    if (
      last &&
      last.kind === "thinking" &&
      last.runId === event.run_id &&
      last.step === state.step
    ) {
      return {
        ...state,
        items: [...items.slice(0, -1), { ...last, text: last.text + event.data.text }],
      };
    }
    return pushItem(state, {
      kind: "thinking",
      id: `thinking:${event.run_id}:${state.step}:${items.length}`,
      runId: event.run_id,
      step: state.step,
      text: event.data.text,
    });
  },

  "tool_call.started": (state, event) =>
    withTool(
      state,
      event.data.tool_call_id,
      (tool) => ({ ...tool, name: event.data.name || tool.name }),
      () => newToolCall(event.data.tool_call_id, event.run_id, state.step, event.data.name),
    ),

  "tool_call.input_delta": (state, event) =>
    withTool(
      state,
      event.data.tool_call_id,
      (tool) => ({ ...tool, inputText: tool.inputText + event.data.json_fragment }),
      () => newToolCall(event.data.tool_call_id, event.run_id, state.step, ""),
    ),

  "tool_call.ready": (state, event) =>
    withTool(
      state,
      event.data.tool_call_id,
      (tool) => ({
        ...tool,
        name: event.data.name || tool.name,
        input: event.data.input,
        phase: tool.phase === "input" ? "ready" : tool.phase,
      }),
      () => newToolCall(event.data.tool_call_id, event.run_id, state.step, event.data.name),
    ),

  "tool_call.output_delta": (state, event) =>
    withTool(
      state,
      event.data.tool_call_id,
      (tool) => ({
        ...appendOutput(tool, event.data.stream, event.data.text),
        phase: tool.phase === "done" ? "done" : "running",
      }),
      () => newToolCall(event.data.tool_call_id, event.run_id, state.step, ""),
    ),

  "tool_call.completed": (state, event) =>
    withTool(
      state,
      event.data.tool_call_id,
      (tool) => {
        const d = event.data;
        let next: ToolCallView = {
          ...tool,
          phase: "done",
          result: d.status,
          exitCode: d.exit_code ?? null,
          timedOut: d.timed_out ?? false,
          durationMs: d.duration_ms,
          peakMemoryBytes: d.peak_memory_bytes ?? null,
          outputBytes: d.output_bytes,
          outputStorageKey: d.output_storage_key ?? null,
        };
        // 실시간 출력이 없었으면(파일 도구, 재접속 후 재생) 완료 이벤트의 발췌를 출력으로 쓴다.
        if (tool.outputChars === 0 && d.output_excerpt) {
          next = {
            ...appendOutput(next, "stdout", d.output_excerpt),
            outputIsExcerpt: d.output_bytes > utf8Length(d.output_excerpt),
          };
        }
        return next;
      },
      () => newToolCall(event.data.tool_call_id, event.run_id, state.step, ""),
    ),

  "file.changed": (state, event) => {
    const file: ChangedFile = {
      path: event.data.path,
      size: event.data.size,
      sessionFileId: event.data.session_file_id ?? null,
      downloadable: event.data.downloadable,
      reason: event.data.reason ?? null,
      runId: event.run_id,
    };
    return { ...state, files: mergeFiles(state.files, [file]) };
  },

  "run.waiting_input": (state, event) => {
    const data = event.data;
    let next: typeof state = {
      ...state,
      status: "waiting_input",
      pendingInteraction: { ...data, runId: event.run_id },
    };
    if (data.kind === "approval" && data.tool_call_id) {
      next = withTool(
        next,
        data.tool_call_id,
        (tool) => ({
          ...tool,
          name: tool.name || data.tool_name || "",
          input: tool.input ?? data.input ?? null,
          phase: "awaiting_approval",
        }),
        () => newToolCall(data.tool_call_id!, event.run_id, state.step, data.tool_name ?? ""),
      );
    }
    return next;
  },

  "interaction.resolved": (state, event) => {
    const data = event.data;
    const pending = state.pendingInteraction;
    const matches = pending?.interaction_id === data.interaction_id;
    let next: typeof state = {
      ...state,
      pendingInteraction: matches ? null : pending,
      sessionAutoApprove: state.sessionAutoApprove || Boolean(data.session_auto_approve),
    };
    const toolCallId = matches ? pending?.tool_call_id : null;
    if (toolCallId && data.kind === "approval") {
      next = withTool(next, toolCallId, (tool) =>
        data.decision === "reject"
          ? { ...tool, phase: "done", result: "rejected" }
          : { ...tool, phase: "ready" },
      );
    }
    return pushItem(next, {
      kind: "interaction",
      id: `interaction:${data.interaction_id}`,
      interactionKind: data.kind,
      decision: data.decision ?? null,
      note: data.note ?? null,
      answer: data.answer ?? null,
      autoApprove: Boolean(data.session_auto_approve),
    });
  },

  "run.resumed": (state, event) => ({
    ...state,
    status: "queued",
    pendingInteraction:
      state.pendingInteraction?.interaction_id === event.data.interaction_id
        ? null
        : state.pendingInteraction,
  }),

  "run.retrying": (state, event) => {
    // 스트림이 끊긴 스텝의 부분 출력은 서버도 버리고 스텝을 다시 실행한다(설계 4.6).
    const { step } = event.data;
    const dropped = new Set<string>();
    for (const tool of Object.values(state.toolCalls)) {
      if (tool.runId === event.run_id && tool.step === step && tool.phase !== "done") {
        dropped.add(tool.id);
      }
    }
    const items = state.items.filter((item) => {
      if (item.kind === "text")
        return !(item.runId === event.run_id && item.step === step && !item.done);
      if (item.kind === "thinking") return !(item.runId === event.run_id && item.step === step);
      if (item.kind === "tool") return !dropped.has(item.toolCallId);
      return true;
    });
    const toolCalls = { ...state.toolCalls };
    for (const id of dropped) delete toolCalls[id];
    const next = { ...state, items, toolCalls };
    return pushItem(next, {
      kind: "notice",
      id: noticeId(next, event, "retrying"),
      code: "retrying",
      message: event.data.reason,
      count: event.data.attempt,
    });
  },

  "context.compacted": (state, event) =>
    pushItem(state, {
      kind: "compaction",
      id: `compaction:${event.data.summary_message_id}`,
      beforeTokens: event.data.before_tokens,
      afterTokens: event.data.after_tokens,
    }),

  "context.prompt_changed": (state, event) =>
    pushItem(state, {
      kind: "notice",
      id: noticeId(state, event, "prompt_changed"),
      code: "prompt_changed",
      message: null,
      count: null,
    }),

  usage: (state, event) => {
    const d = event.data;
    const usage = state.usage;
    return {
      ...state,
      model: d.model || state.model,
      usage: {
        inputTokens: usage.inputTokens + d.input_tokens,
        outputTokens: usage.outputTokens + d.output_tokens,
        reasoningTokens: usage.reasoningTokens + (d.reasoning_tokens ?? 0),
        cachedInputTokens: usage.cachedInputTokens + (d.cached_input_tokens ?? 0),
        costUsd:
          d.cost_usd === undefined || d.cost_usd === null
            ? usage.costUsd
            : (usage.costUsd ?? 0) + d.cost_usd,
        estimated: usage.estimated || d.estimated,
      },
    };
  },

  "message.completed": (state, event) => {
    const { message_id: messageId, text } = event.data;
    // 런의 마지막 메시지에 그 런이 바꾼 파일이 온다. 대개 file.changed로 이미 받은 것들이다.
    const files = mergeFiles(
      state.files,
      (event.data.files ?? []).map((file) => changedFileOf(file, event.run_id)),
    );
    const indexes: number[] = [];
    state.items.forEach((item, index) => {
      if (item.kind === "text" && item.messageId === messageId) indexes.push(index);
    });
    if (indexes.length === 0) {
      if (!text) return { ...state, files };
      const item: TimelineItem = {
        kind: "text",
        id: `text:${messageId}:0`,
        runId: event.run_id,
        messageId,
        step: state.step,
        text,
        done: true,
      };
      // 재생(events?after=0)에는 델타가 없어 글이 같은 스텝의 도구 카드보다 늦게 온다. 모델은 글을 쓴 뒤
      // 도구를 부르므로 그 스텝의 첫 도구 카드 앞에 둔다.
      const at = state.items.findIndex((entry) => {
        if (entry.kind !== "tool") return false;
        const tool = state.toolCalls[entry.toolCallId];
        return tool?.runId === event.run_id && tool.step === state.step;
      });
      const items =
        at === -1
          ? [...state.items, item]
          : [...state.items.slice(0, at), item, ...state.items.slice(at)];
      return { ...state, files, items };
    }
    const segments = indexes.map((i) => state.items[i] as Extract<TimelineItem, { kind: "text" }>);
    const joined = segments.map((s) => s.text).join("");
    // 델타를 일부 놓쳤으면(재접속) 완료 이벤트의 전문을 첫 조각에 넣고 나머지 조각을 지운다.
    const items =
      joined === text
        ? state.items.map((item, i) =>
            indexes.includes(i) && item.kind === "text" ? { ...item, done: true } : item,
          )
        : state.items
            .map((item, i) =>
              i === indexes[0] && item.kind === "text" ? { ...item, text, done: true } : item,
            )
            .filter((_, i) => !indexes.slice(1).includes(i));
    return { ...state, files, items };
  },

  "run.completed": (state, event) => {
    const d = event.data;
    const waiting = d.status === "waiting_input";
    let next: typeof state = {
      ...state,
      status: d.status,
      usage: usageFromTotal(d.usage_total),
      queuePosition: null,
      endedAt: waiting ? null : event.ts,
      pendingInteraction: waiting ? state.pendingInteraction : null,
      items: markTextDone(state.items, event.run_id),
      toolCalls: waiting ? state.toolCalls : closeOpenTools(state.toolCalls, event.run_id),
      activeChild: null,
    };
    // run.error를 놓쳤으면(재접속 중 끊김) 완료 이벤트에 담긴 오류로 보여 준다.
    if (d.status === "failed" && d.error && !state.error) {
      const error: RunError = {
        code: d.error.code,
        message: d.error.message,
        retryable: d.error.retryable ?? false,
      };
      next = pushItem({ ...next, error }, { kind: "error", id: `error:${event.run_id}`, ...error });
    }
    if (d.status === "cancelled") {
      const timeout = d.error?.code === "input_timeout";
      next = pushItem(next, {
        kind: "notice",
        id: noticeId(next, event, "cancelled"),
        code: timeout ? "input_timeout" : "stopped",
        message: timeout ? (d.error?.message ?? null) : null,
        count: null,
      });
    }
    if (d.limit === "steps") {
      next = pushItem(next, {
        kind: "notice",
        id: noticeId(next, event, "step_limit"),
        code: "step_limit",
        message: null,
        count: d.steps,
      });
    }
    if (d.changes_truncated) {
      next = pushItem(next, {
        kind: "notice",
        id: noticeId(next, event, "changes_truncated"),
        code: "changes_truncated",
        message: null,
        count: null,
      });
    }
    return next;
  },

  // 하위 에이전트 호출. 부모 런은 열린 채로 남고 자식의 진행은 child.sessionId 세션의 스트림으로 따로
  // 받는다. 자식이 끝나면 child_run.completed 뒤에 재개된 부모의 tool_call.completed가 카드를 닫는다.
  "child_run.started": (state, event) => {
    const d = event.data;
    return withTool(
      state,
      d.tool_call_id,
      (tool) => ({
        ...tool,
        name: tool.name || CALL_AGENT,
        phase: tool.phase === "done" ? "done" : "child",
        child: {
          runId: d.child_run_id,
          sessionId: d.child_session_id,
          agentId: d.agent_id,
          agentSlug: d.agent_slug,
          agentName: d.agent_name,
          version: d.version,
          depth: d.depth,
          resultFormat: d.result_format,
          taskPreview: d.task_preview,
          end: tool.child?.runId === d.child_run_id ? tool.child.end : null,
        },
      }),
      () => newToolCall(d.tool_call_id, event.run_id, state.step, CALL_AGENT),
    );
  },

  "child_run.completed": (state, event) => {
    const d = event.data;
    const next = withTool(state, d.tool_call_id, (tool) =>
      tool.child
        ? {
            ...tool,
            child: {
              ...tool.child,
              end: {
                status: d.status,
                error: d.error
                  ? {
                      code: d.error.code,
                      message: d.error.message,
                      retryable: Boolean(d.error.retryable),
                    }
                  : null,
                steps: d.steps,
                usage: usageFromTotal(d.usage_total),
                durationMs: d.duration_ms,
                files: d.files,
              },
            },
          }
        : tool,
    );
    return state.activeChild?.toolCallId === d.tool_call_id ? { ...next, activeChild: null } : next;
  },

  // 실패한 런은 run.error 바로 뒤에 run.completed(failed)로 끝난다. 상태 전환은 여기서 먼저 한다.
  "run.error": (state, event) => {
    const error: RunError = {
      code: event.data.code,
      message: event.data.message,
      retryable: event.data.retryable ?? false,
    };
    const next = {
      ...state,
      error,
      status: state.status === "cancelled" ? state.status : ("failed" as const),
      endedAt: event.ts,
      queuePosition: null,
      pendingInteraction: null,
      items: markTextDone(state.items, event.run_id),
    };
    return pushItem(next, { kind: "error", id: `error:${event.run_id}`, ...error });
  },
};

export function applyRunEvent<S extends RunViewState>(state: S, event: RunEvent): S {
  if (event.type === "ping") return state;
  const handler = handlers[event.type] as Handler<RunEventType> | undefined;
  if (!handler) return state;
  const runId = event.run_id;
  const seen = state.lastSeq[runId] ?? 0;
  if (event.seq > 0 && event.seq <= seen) return state;
  let next: S = {
    ...state,
    lastSeq: { ...state.lastSeq, [runId]: Math.max(seen, event.seq) },
  };
  if (state.runId !== runId) next = startRun(next, runId, event.session_id);
  return handler(next, event as RunEventOf<RunEventType>);
}

export function applyRunEvents<S extends RunViewState>(state: S, events: readonly RunEvent[]): S {
  return events.reduce<S>((acc, event) => applyRunEvent(acc, event), state);
}

const DELTA_TYPES = new Set<string>([
  "message.delta",
  "thinking.delta",
  "tool_call.input_delta",
  "tool_call.output_delta",
]);

export interface RunStoreActions {
  // 델타 이벤트는 다음 애니메이션 프레임에 모아서 반영하고, 그 밖의 이벤트는 쌓인 것과 함께 바로 반영한다.
  applyEvent: (event: RunEvent) => void;
  applyEvents: (events: readonly RunEvent[]) => void;
  flush: () => void;
  // 보낸 메시지를 서버 응답 전에 먼저 보여 준다. 만든 항목의 id를 돌려준다.
  addUserMessage: (text: string, options?: { id?: string; files?: AttachmentView[] }) => string;
  removeItem: (id: string) => void;
  setSessionAutoApprove: (on: boolean) => void;
  reset: (partial?: Partial<RunViewState>) => void;
}

export type RunStoreState = RunViewState & { actions: RunStoreActions };
export type RunStore = StoreApi<RunStoreState>;

type Schedule = (callback: () => void) => () => void;

const frameSchedule: Schedule = (callback) => {
  if (typeof requestAnimationFrame === "function") {
    const handle = requestAnimationFrame(() => callback());
    return () => cancelAnimationFrame(handle);
  }
  const handle = setTimeout(callback, 16);
  return () => clearTimeout(handle);
};

let userMessageCounter = 0;

export function createRunStore(
  options: { initial?: Partial<RunViewState>; schedule?: Schedule } = {},
): RunStore {
  const schedule = options.schedule ?? frameSchedule;
  let queue: RunEvent[] = [];
  let cancelScheduled: (() => void) | null = null;

  return createStore<RunStoreState>()((set) => {
    const flush = () => {
      cancelScheduled?.();
      cancelScheduled = null;
      if (queue.length === 0) return;
      const events = queue;
      queue = [];
      set((state) => applyRunEvents(state, events));
    };

    const actions: RunStoreActions = {
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
      addUserMessage(text, { id, files } = {}) {
        flush();
        userMessageCounter += 1;
        const itemId = id ?? `user:local:${userMessageCounter}`;
        set((state) => ({
          items: [
            ...state.items,
            files?.length
              ? { kind: "user", id: itemId, text, files }
              : { kind: "user", id: itemId, text },
          ],
        }));
        return itemId;
      },
      removeItem(id) {
        set((state) => ({ items: state.items.filter((item) => item.id !== id) }));
      },
      setSessionAutoApprove(on) {
        set({ sessionAutoApprove: on });
      },
      reset(partial) {
        cancelScheduled?.();
        cancelScheduled = null;
        queue = [];
        set({ ...initialRunState(partial) });
      },
    };

    return { ...initialRunState(options.initial), actions };
  });
}
