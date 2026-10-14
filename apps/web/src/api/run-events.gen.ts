// packages/event-schema/run-events.schema.json에서 scripts/gen-run-events.ts가 만든 파일이다. 손으로 고치지 않는다.

export const RUN_EVENT_SCHEMA_VERSION = 2;
export const TERMINAL_EVENT_TYPES = ["run.completed", "workflow.completed"] as const;
export const TRANSIENT_EVENT_TYPES = [
  "answer.delta",
  "message.delta",
  "node.progress",
  "node.text_delta",
  "node.waiting",
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
  "child_run.started",
  "child_run.completed",
  "workflow.queued",
  "workflow.started",
  "node.started",
  "node.text_delta",
  "node.progress",
  "node.waiting",
  "node.retrying",
  "node.usage",
  "node.completed",
  "iteration.started",
  "iteration.next",
  "iteration.completed",
  "answer.delta",
  "answer.completed",
  "workflow.waiting_input",
  "workflow.resumed",
  "workflow.error",
  "workflow.completed",
  "ping",
] as const;

export interface AnswerCompletedData {
  text: string;
  message_id?: string | null;
}

export interface AnswerDeltaData {
  text: string;
  node_id: string;
  node_run_id?: string | null;
}

/** 자식 런이 끝나 부모가 다시 큐에 들어간다. 결과는 뒤이은 tool_call.completed에 있다. */
export interface ChildRunCompletedData {
  tool_call_id: string;
  child_run_id: string;
  status: "succeeded" | "failed" | "cancelled";
  error?: RunErrorData | null;
  steps: number;
  usage_total: UsageTotal;
  duration_ms: number;
  files: number;
}

/** call_agent가 자식 런을 만들고 부모가 그 결과를 기다리기 시작했다(오케스트레이션 6.1).

부모 런은 run.completed 없이 열린 채로 남는다. 자식의 진행과 승인은 child_session_id 세션의
이벤트 스트림으로 따로 받는다. */
export interface ChildRunStartedData {
  tool_call_id: string;
  child_run_id: string;
  child_session_id: string;
  agent_id: string;
  agent_slug: string;
  agent_name: string;
  version: "draft" | "published";
  depth: number;
  result_format: "text" | "json";
  task_preview: string;
}

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

export interface IterationCompletedData {
  node_id: string;
  count: number;
  failed_count: number;
  duration_ms: number;
}

export interface IterationNextData {
  node_id: string;
  index: number;
}

export interface IterationStartedData {
  node_id: string;
  total: number;
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

export interface NodeCompletedData {
  node_run_id: string;
  node_id: string;
  status: "succeeded" | "failed" | "skipped" | "cancelled" | "waiting_input";
  outputs_preview?: Record<string, unknown>;
  truncated?: boolean;
  error?: NodeError | null;
  usage?: UsageTotal | null;
  duration_ms: number;
  branch?: string | null;
  warnings?: string[];
}

export interface NodeError {
  code: string;
  message: string;
  retryable?: boolean;
}

/** agent 노드의 자식 런 진행. 자식을 만들자마자 한 번, 그 뒤 10초마다 온다. */
export interface NodeProgressData {
  node_id: string;
  node_run_id: string;
  steps?: number | null;
  usage?: UsageTotal | null;
  position?: number | null;
  child_run_id?: string | null;
  child_session_id?: string | null;
}

export interface NodeRetryingData {
  node_id: string;
  node_run_id: string;
  attempt: number;
  reason: string;
}

export interface NodeStartedData {
  node_run_id: string;
  node_id: string;
  node_type: string;
  title: string;
  index: number;
  iteration_id?: string | null;
  iteration_index?: number | null;
  attempt: number;
  child_run_id?: string | null;
  child_session_id?: string | null;
}

export interface NodeTextDeltaData {
  node_id: string;
  node_run_id: string;
  text: string;
}

/** 모델을 부른 노드(llm 등)의 호출 한 번. 1단계 usage와 같은 값에 노드가 붙고 step이 없다. */
export interface NodeUsageData {
  node_id: string;
  node_run_id: string;
  model: string;
  key_index?: number | null;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens?: number | null;
  cached_input_tokens?: number | null;
  estimated: boolean;
  cost_usd?: number | null;
}

export interface NodeWaitingData {
  node_id: string;
  node_run_id?: string | null;
  reason: "queue" | "rate_limit";
  position?: number | null;
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
  child_run_id?: string | null;
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

export interface WorkflowCompletedData {
  status: "succeeded" | "failed" | "cancelled" | "waiting_input";
  outputs?: Record<string, unknown> | null;
  truncated?: boolean;
  usage_total: UsageTotal;
  node_runs: number;
  duration_ms: number;
  error?: WorkflowErrorData | null;
}

export interface WorkflowErrorData {
  code: string;
  message: string;
  node_id?: string | null;
  retryable?: boolean;
}

export interface WorkflowQueuedData {
  position: number;
}

export interface WorkflowResumedData {
  node_id: string;
}

export interface WorkflowStartedData {
  workflow_id?: string | null;
  version_id?: string | null;
  kind: "workflow" | "chatflow";
  inputs_preview: Record<string, unknown>;
  truncated: boolean;
  resumed: boolean;
  node_count: number;
}

export interface WorkflowWaitingInputData {
  node_id: string;
  iteration_index?: number | null;
  child_run_id?: string | null;
  interaction: RunWaitingInputData;
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
  "child_run.started": ChildRunStartedData;
  "child_run.completed": ChildRunCompletedData;
  "workflow.queued": WorkflowQueuedData;
  "workflow.started": WorkflowStartedData;
  "node.started": NodeStartedData;
  "node.text_delta": NodeTextDeltaData;
  "node.progress": NodeProgressData;
  "node.waiting": NodeWaitingData;
  "node.retrying": NodeRetryingData;
  "node.usage": NodeUsageData;
  "node.completed": NodeCompletedData;
  "iteration.started": IterationStartedData;
  "iteration.next": IterationNextData;
  "iteration.completed": IterationCompletedData;
  "answer.delta": AnswerDeltaData;
  "answer.completed": AnswerCompletedData;
  "workflow.waiting_input": WorkflowWaitingInputData;
  "workflow.resumed": WorkflowResumedData;
  "workflow.error": WorkflowErrorData;
  "workflow.completed": WorkflowCompletedData;
  ping: PingData;
}
