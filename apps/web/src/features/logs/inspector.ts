import type {
  InteractionKind,
  MessageFileData,
  RunEventOf,
  RunEventType,
  ToolCallResult,
} from "@/api/run-events";
import type { RunEventOut } from "./types";

// 런 인스펙터(설계 7.3 RunInspector)가 그리는 모양. SQLite에 남은 영속 이벤트만으로 스텝·도구 호출·
// 승인 결정·사용량을 다시 짠다. 델타 이벤트는 저장되지 않으므로 도구 출력은 tool_call.completed의 발췌다.
type DataOf<T extends RunEventType> = RunEventOf<T>["data"];

export interface InspectorUsage {
  model: string;
  keyIndex: number | null;
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number | null;
  cachedInputTokens: number | null;
  costUsd: number | null;
  estimated: boolean;
}

export interface InspectorResolution {
  decision: "approve" | "reject" | null;
  note: string | null;
  answer: string | null;
  autoApprove: boolean;
  resolvedAt: string;
}

export interface InspectorInteraction {
  interactionId: string;
  kind: InteractionKind;
  toolCallId: string | null;
  toolName: string | null;
  question: string | null;
  requestedAt: string | null;
  resolution: InspectorResolution | null;
}

// call_agent 호출이 만든 하위 에이전트 런(child_run.started·completed)
export interface InspectorChildRun {
  runId: string;
  sessionId: string;
  agentSlug: string;
  agentName: string;
  version: "draft" | "published";
  status: "succeeded" | "failed" | "cancelled" | null;
}

export interface InspectorToolCall {
  id: string;
  name: string;
  input: Record<string, unknown> | null;
  // null이면 아직 끝나지 않았다(실행 중이거나 승인을 기다린다).
  status: ToolCallResult | null;
  exitCode: number | null;
  timedOut: boolean;
  durationMs: number | null;
  peakMemoryBytes: number | null;
  outputExcerpt: string;
  outputBytes: number | null;
  // 256KB를 넘어 storage에 전문이 따로 있다.
  outputStored: boolean;
  interaction: InspectorInteraction | null;
  child: InspectorChildRun | null;
}

export interface InspectorMessage {
  id: string;
  text: string;
  stopReason: string;
  files: MessageFileData[];
}

export interface InspectorNotice {
  seq: number;
  ts: string;
  type: string;
  data: Record<string, unknown>;
}

export interface InspectorFile {
  path: string;
  size: number;
  sessionFileId: string | null;
  downloadable: boolean;
  reason: string | null;
}

export interface InspectorStep {
  step: number;
  startedAt: string | null;
  usage: InspectorUsage | null;
  toolCalls: InspectorToolCall[];
  messages: InspectorMessage[];
  notices: InspectorNotice[];
}

export interface RunInspection {
  started: DataOf<"run.started"> | null;
  // 승인·답을 받아 다시 이어 간 횟수
  resumes: number;
  // 첫 스텝 전에 생긴 일(작업공간 복원 등)
  preamble: InspectorNotice[];
  steps: InspectorStep[];
  interactions: InspectorInteraction[];
  files: InspectorFile[];
  error: DataOf<"run.error"> | null;
  completed: DataOf<"run.completed"> | null;
}

// 스텝이나 도구 호출과 따로 보여 줄 필요가 없는 이벤트. 요약(started·completed)이나 승인 카드로 그린다.
const SUMMARIZED = new Set([
  "run.queued",
  "run.started",
  "run.completed",
  "run.waiting_input",
  "interaction.resolved",
  "step.started",
  "tool_call.started",
  "tool_call.ready",
  "tool_call.completed",
  "usage",
  "message.completed",
  "file.changed",
  "message.delta",
  "thinking.delta",
  "tool_call.input_delta",
  "tool_call.output_delta",
  "child_run.started",
  "child_run.completed",
  "ping",
]);

// 서버 응답의 data는 이벤트마다 모양이 다른 사전이다. type을 보고 그 이벤트의 모양으로 읽는다.
function dataOf<T extends RunEventType>(data: RunEventOut["data"]): DataOf<T> {
  return data as unknown as DataOf<T>;
}

function newTool(id: string, name: string): InspectorToolCall {
  return {
    id,
    name,
    input: null,
    status: null,
    exitCode: null,
    timedOut: false,
    durationMs: null,
    peakMemoryBytes: null,
    outputExcerpt: "",
    outputBytes: null,
    outputStored: false,
    interaction: null,
    child: null,
  };
}

function newStep(step: number, startedAt: string | null): InspectorStep {
  return { step, startedAt, usage: null, toolCalls: [], messages: [], notices: [] };
}

export function inspectRun(events: readonly RunEventOut[]): RunInspection {
  const result: RunInspection = {
    started: null,
    resumes: 0,
    preamble: [],
    steps: [],
    interactions: [],
    files: [],
    error: null,
    completed: null,
  };
  const tools = new Map<string, InspectorToolCall>();
  const interactions = new Map<string, InspectorInteraction>();
  let current: InspectorStep | null = null;

  // 스텝 표시 전에 도구 이벤트가 오는 일은 없지만, 기록이 일부만 남았을 때도 버리지 않는다.
  const stepForTools = (): InspectorStep => {
    if (current === null) {
      current = newStep(0, null);
      result.steps.push(current);
    }
    return current;
  };
  const toolFor = (id: string, name: string | null): InspectorToolCall => {
    let tool = tools.get(id);
    if (tool === undefined) {
      tool = newTool(id, name ?? "");
      tools.set(id, tool);
      stepForTools().toolCalls.push(tool);
    } else if (name && !tool.name) {
      tool.name = name;
    }
    return tool;
  };

  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  for (const event of ordered) {
    const data = event.data;
    switch (event.type) {
      case "run.started":
        result.started = dataOf<"run.started">(data);
        break;
      case "run.resumed":
        result.resumes += 1;
        break;
      case "step.started": {
        const { step } = dataOf<"step.started">(data);
        current = newStep(step, event.ts);
        result.steps.push(current);
        break;
      }
      case "tool_call.started": {
        const started = dataOf<"tool_call.started">(data);
        toolFor(started.tool_call_id, started.name);
        break;
      }
      case "tool_call.ready": {
        const ready = dataOf<"tool_call.ready">(data);
        toolFor(ready.tool_call_id, ready.name).input = ready.input;
        break;
      }
      case "tool_call.completed": {
        const done = dataOf<"tool_call.completed">(data);
        const tool = toolFor(done.tool_call_id, null);
        tool.status = done.status;
        tool.exitCode = done.exit_code ?? null;
        tool.timedOut = done.timed_out ?? false;
        tool.durationMs = done.duration_ms;
        tool.peakMemoryBytes = done.peak_memory_bytes ?? null;
        tool.outputExcerpt = done.output_excerpt;
        tool.outputBytes = done.output_bytes;
        tool.outputStored = Boolean(done.output_storage_key);
        break;
      }
      case "run.waiting_input": {
        const waiting = dataOf<"run.waiting_input">(data);
        const interaction: InspectorInteraction = {
          interactionId: waiting.interaction_id,
          kind: waiting.kind,
          toolCallId: waiting.tool_call_id ?? null,
          toolName: waiting.tool_name ?? null,
          question: waiting.question ?? null,
          requestedAt: event.ts,
          resolution: interactions.get(waiting.interaction_id)?.resolution ?? null,
        };
        interactions.set(interaction.interactionId, interaction);
        if (interaction.toolCallId) {
          toolFor(interaction.toolCallId, interaction.toolName).interaction = interaction;
        }
        break;
      }
      case "interaction.resolved": {
        const resolved = dataOf<"interaction.resolved">(data);
        let interaction = interactions.get(resolved.interaction_id);
        if (interaction === undefined) {
          interaction = {
            interactionId: resolved.interaction_id,
            kind: resolved.kind,
            toolCallId: null,
            toolName: null,
            question: null,
            requestedAt: null,
            resolution: null,
          };
          interactions.set(interaction.interactionId, interaction);
        }
        interaction.resolution = {
          decision: resolved.decision ?? null,
          note: resolved.note ?? null,
          answer: resolved.answer ?? null,
          autoApprove: resolved.session_auto_approve ?? false,
          resolvedAt: event.ts,
        };
        break;
      }
      case "usage": {
        const usage = dataOf<"usage">(data);
        const step = result.steps.find((item) => item.step === usage.step) ?? stepForTools();
        step.usage = {
          model: usage.model,
          keyIndex: usage.key_index ?? null,
          inputTokens: usage.input_tokens,
          outputTokens: usage.output_tokens,
          reasoningTokens: usage.reasoning_tokens ?? null,
          cachedInputTokens: usage.cached_input_tokens ?? null,
          costUsd: usage.cost_usd ?? null,
          estimated: usage.estimated,
        };
        break;
      }
      case "message.completed": {
        const message = dataOf<"message.completed">(data);
        stepForTools().messages.push({
          id: message.message_id,
          text: message.text,
          stopReason: message.stop_reason,
          files: message.files ?? [],
        });
        break;
      }
      case "file.changed": {
        const file = dataOf<"file.changed">(data);
        result.files.push({
          path: file.path,
          size: file.size,
          sessionFileId: file.session_file_id ?? null,
          downloadable: file.downloadable,
          reason: file.reason ?? null,
        });
        break;
      }
      case "child_run.started": {
        const started = dataOf<"child_run.started">(data);
        toolFor(started.tool_call_id, "call_agent").child = {
          runId: started.child_run_id,
          sessionId: started.child_session_id,
          agentSlug: started.agent_slug,
          agentName: started.agent_name,
          version: started.version,
          status: null,
        };
        break;
      }
      case "child_run.completed": {
        const ended = dataOf<"child_run.completed">(data);
        const tool = tools.get(ended.tool_call_id);
        if (tool?.child) tool.child.status = ended.status;
        break;
      }
      case "run.error":
        result.error = dataOf<"run.error">(data);
        break;
      case "run.completed":
        result.completed = dataOf<"run.completed">(data);
        break;
      default:
        break;
    }
    if (!SUMMARIZED.has(event.type)) {
      const notice: InspectorNotice = { seq: event.seq, ts: event.ts, type: event.type, data };
      if (current === null) result.preamble.push(notice);
      else current.notices.push(notice);
    }
  }
  result.interactions = [...interactions.values()];
  return result;
}

export function stepTokens(step: InspectorStep): number {
  return step.usage ? step.usage.inputTokens + step.usage.outputTokens : 0;
}
