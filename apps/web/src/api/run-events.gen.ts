// packages/event-schema/run-events.schema.json에서 scripts/gen-run-events.ts가 만든 파일이다. 손으로 고치지 않는다.

export const RUN_EVENT_SCHEMA_VERSION = 1;
export const TERMINAL_EVENT_TYPE = "run.completed";
export const TRANSIENT_EVENT_TYPES = [
  "message.delta",
  "ping",
  "thinking.delta",
  "tool_call.input_delta",
  "tool_call.output_delta",
] as const;
export const RUN_EVENT_TYPES = [
  "run.queued",
  "run.started",
  "workspace.restoring",
  "workspace.restored",
  "workspace.restore_warning",
  "workspace.recreated",
  "step.started",
  "message.delta",
  "thinking.delta",
  "tool_call.started",
  "tool_call.input_delta",
  "tool_call.ready",
  "tool_call.output_delta",
  "tool_call.completed",
  "file.changed",
  "run.waiting_input",
  "interaction.resolved",
  "run.resumed",
  "run.retrying",
  "context.compacted",
  "context.prompt_changed",
  "usage",
  "message.completed",
  "run.completed",
  "run.error",
  "ping",
] as const;

export interface ContextCompactedData {
  before_tokens: number;
  after_tokens: number;
  summary_message_id: string;
}

export interface ContextPromptChangedData {
  previous_hash?: string | null;
  prompt_hash: string;
}

export interface FileChangedData {
  path: string;
  size: number;
  session_file_id?: string | null;
  downloadable: boolean;
  reason?: string | null;
}

export interface InteractionResolvedData {
  interaction_id: string;
  kind: "question" | "approval";
  decision?: "approve" | "reject" | null;
  note?: string | null;
  answer?: string | null;
  session_auto_approve?: boolean | null;
}

export interface MessageCompletedData {
  message_id: string;
  text: string;
  stop_reason: string;
  files?: MessageFileData[];
}

export interface MessageDeltaData {
  message_id: string;
  text: string;
}

export interface MessageFileData {
  session_file_id?: string | null;
  path: string;
  size: number;
  downloadable: boolean;
  filename?: string | null;
  mime?: string | null;
}

export type PingData = Record<string, never>;

export interface RunCompletedData {
  status: "succeeded" | "failed" | "cancelled" | "waiting_input";
  usage_total: UsageTotal;
  steps: number;
  changes_truncated?: boolean | null;
  output?: unknown;
  limit?: "steps" | null;
  error?: RunErrorData | null;
}

export interface RunErrorData {
  code: string;
  message: string;
  retryable?: boolean;
}

export interface RunQueuedData {
  position: number;
}

export interface RunResumedData {
  interaction_id: string;
}

export interface RunRetryingData {
  step: number;
  reason: string;
  attempt: number;
}

export interface RunStartedData {
  step_limit: number;
  model: string | null;
  workspace_instance_id: string | null;
  resumed: boolean;
  approval_mode: "off" | "http_writes" | "all_shell";
}

export interface RunWaitingInputData {
  interaction_id: string;
  kind: "question" | "approval";
  question?: string | null;
  options?: string[] | null;
  allow_free_text?: boolean | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  input?: Record<string, unknown> | null;
  cwd?: string | null;
  timeout_sec?: number | null;
}

export interface StepStartedData {
  step: number;
}

export interface ThinkingDeltaData {
  text: string;
}

export interface ToolCallCompletedData {
  tool_call_id: string;
  status: "ok" | "error" | "timeout" | "cancelled" | "rejected";
  exit_code?: number | null;
  timed_out?: boolean;
  duration_ms: number;
  peak_memory_bytes?: number | null;
  output_excerpt: string;
  output_bytes: number;
  output_storage_key?: string | null;
}

export interface ToolCallInputDeltaData {
  tool_call_id: string;
  json_fragment: string;
}

export interface ToolCallOutputDeltaData {
  tool_call_id: string;
  stream: "stdout" | "stderr";
  text: string;
}

export interface ToolCallReadyData {
  tool_call_id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolCallStartedData {
  tool_call_id: string;
  name: string;
}

export interface UsageData {
  step: number;
  model: string;
  key_index?: number | null;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens?: number | null;
  cached_input_tokens?: number | null;
  estimated: boolean;
  cost_usd?: number | null;
}

/** run.usage 열과 run.completed.usage_total의 구조. */
export interface UsageTotal {
  input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  cached_input_tokens?: number;
  cost_usd?: number | null;
  estimated?: boolean;
}

export interface WorkspaceEventData {
  snapshot_id?: string | null;
  packages?: number | null;
  message?: string | null;
}

export interface RunEventDataMap {
  "run.queued": RunQueuedData;
  "run.started": RunStartedData;
  "workspace.restoring": WorkspaceEventData;
  "workspace.restored": WorkspaceEventData;
  "workspace.restore_warning": WorkspaceEventData;
  "workspace.recreated": WorkspaceEventData;
  "step.started": StepStartedData;
  "message.delta": MessageDeltaData;
  "thinking.delta": ThinkingDeltaData;
  "tool_call.started": ToolCallStartedData;
  "tool_call.input_delta": ToolCallInputDeltaData;
  "tool_call.ready": ToolCallReadyData;
  "tool_call.output_delta": ToolCallOutputDeltaData;
  "tool_call.completed": ToolCallCompletedData;
  "file.changed": FileChangedData;
  "run.waiting_input": RunWaitingInputData;
  "interaction.resolved": InteractionResolvedData;
  "run.resumed": RunResumedData;
  "run.retrying": RunRetryingData;
  "context.compacted": ContextCompactedData;
  "context.prompt_changed": ContextPromptChangedData;
  usage: UsageData;
  "message.completed": MessageCompletedData;
  "run.completed": RunCompletedData;
  "run.error": RunErrorData;
  ping: PingData;
}
