import type { ApprovalMode } from "@/api/types";
import {
  appendOutput,
  changedFileOf,
  mergeFiles,
  newToolCall,
  usageFromTotal,
  type AttachmentView,
  type ChangedFile,
  type PendingInteraction,
  type RunStatus,
  type RunViewState,
  type TimelineItem,
  type ToolCallView,
} from "@/stores/runStore";
import type { MessageOut, PendingInteractionOut, RunOut, SessionDetail } from "./types";

// 거절된 도구 호출의 tool_result 머리말(apps/api/app/agent/interactions.py REJECTED_TEXT).
const REJECTED_PREFIX = "사용자가 이 작업을 거절했습니다";
const CONTEXT_BLOCK_KIND = "context";
const APPROVAL_MODES = new Set<string>(["off", "http_writes", "all_shell"]);

type Block = MessageOut["blocks"][number];

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

// user 행 끝의 kind=context 블록은 실행 정보라 보여 주지 않는다. text 열이 사용자 글이다.
function userText(row: MessageOut): string {
  if (row.text) return row.text;
  return row.blocks
    .filter((block) => block.type === "text" && block.kind !== CONTEXT_BLOCK_KIND)
    .map((block) => str(block.text))
    .join("\n");
}

export function attachmentsOf(row: Pick<MessageOut, "files">): AttachmentView[] {
  return row.files.map((file) => ({
    sessionFileId: file.session_file_id ?? null,
    name: file.filename || basename(file.path),
    size: file.size,
    downloadable: file.downloadable,
  }));
}

function restoredTool(
  use: Block,
  result: Block | undefined,
  runId: string,
  id: string,
): ToolCallView {
  const input = record(use.input);
  let tool: ToolCallView = {
    ...newToolCall(id, runId, 0, str(use.name)),
    input,
    restored: true,
    phase: "done",
  };
  if (!result) {
    // 결과가 없으면 실행하기 전에 런이 끝난 호출이다(중지·실패·종료).
    return { ...tool, result: "cancelled" };
  }
  const content = str(result.content);
  tool = appendOutput(tool, "stdout", content);
  if (content.startsWith(REJECTED_PREFIX)) return { ...tool, result: "rejected" };
  return { ...tool, result: result.is_error === true ? "error" : "ok" };
}

function pendingOf(pending: PendingInteractionOut, runId: string): PendingInteraction {
  return {
    interaction_id: pending.interaction_id,
    kind: pending.kind,
    question: pending.question ?? null,
    options: pending.options ?? null,
    allow_free_text: pending.allow_free_text ?? null,
    tool_call_id: pending.tool_call_id ?? null,
    tool_name: pending.tool_name ?? null,
    input: pending.input ?? null,
    cwd: pending.cwd ?? null,
    timeout_sec: pending.timeout_sec ?? null,
    runId,
  };
}

function approvalModeOf(value: string): ApprovalMode | null {
  return APPROVAL_MODES.has(value) ? (value as ApprovalMode) : null;
}

function numberField(value: Record<string, unknown>, key: string): number | null {
  const found = value[key];
  return typeof found === "number" ? found : null;
}

// 진행 중인 런은 events?after=0 재생이 다시 그리므로 상태 표시만 먼저 맞춘다. 사용량은 재생에서 더한다.
// 하위 에이전트를 기다리는 런은 서버가 running과 child로 알려 준다(오케스트레이션 6.4).
function activeRunState(run: RunOut): Partial<RunViewState> {
  const child = run.child ?? null;
  return {
    runId: run.id,
    status: run.status as RunStatus,
    pendingInteraction: run.pending_interaction ? pendingOf(run.pending_interaction, run.id) : null,
    approvalMode: approvalModeOf(run.approval_mode),
    activeChild: child
      ? {
          toolCallId: child.tool_call_id,
          runId: child.run_id,
          sessionId: child.session_id,
          agentSlug: child.agent_slug,
          agentName: child.agent_name,
        }
      : null,
  };
}

function finishedRunState(run: RunOut): Partial<RunViewState> {
  return {
    runId: run.id,
    status: run.status as RunStatus,
    usage: usageFromTotal(run.usage),
    step: run.steps,
    stepLimit: numberField(run.limits, "max_steps"),
    model: str(run.model.used) || str(run.model.model_id) || null,
    approvalMode: approvalModeOf(run.approval_mode),
    startedAt: run.started_at,
    endedAt: run.ended_at,
    error: run.error
      ? {
          code: run.error.code,
          message: run.error.message,
          retryable: run.error.retryable ?? false,
        }
      : null,
  };
}

export interface HistoryOptions {
  // 서버가 더 앞의 메시지를 남겨 두었다(MessageList.has_more).
  hasMore?: boolean;
}

/*
 * GET /sessions/{sid}/messages(+ GET /sessions/{sid})로 채팅 화면 상태를 만든다. 메시지 행 하나가 스텝
 * 하나이고 assistant 행의 blocks는 text·tool_use 뒤에 tool_result가 붙는다(설계 12.2 M6 이후 확정).
 * 진행 중인 런의 assistant 행은 빼고 이벤트 재생(events?after=0)이 다시 그리게 한다.
 */
export function historyState(
  messages: readonly MessageOut[],
  detail: SessionDetail | null,
  options: HistoryOptions = {},
): Partial<RunViewState> {
  const active = detail?.active_run ?? null;
  const items: TimelineItem[] = [];
  const toolCalls: Record<string, ToolCallView> = {};
  let files: ChangedFile[] = [];

  if (options.hasMore) {
    items.push({
      kind: "notice",
      id: "notice:history_truncated",
      code: "history_truncated",
      message: null,
      count: null,
    });
  }

  for (const row of messages) {
    if (row.role === "user") {
      const attachments = attachmentsOf(row);
      items.push(
        attachments.length > 0
          ? { kind: "user", id: `user:${row.id}`, text: userText(row), files: attachments }
          : { kind: "user", id: `user:${row.id}`, text: userText(row) },
      );
      continue;
    }
    if (row.role === "system_summary") {
      items.push({
        kind: "compaction",
        id: `compaction:${row.id}`,
        beforeTokens: null,
        afterTokens: null,
      });
      continue;
    }
    const runId = row.run_id ?? "";
    if (active && runId === active.id) continue;
    const results = new Map<string, Block>();
    for (const block of row.blocks) {
      if (block.type === "tool_result") results.set(str(block.tool_use_id), block);
    }
    row.blocks.forEach((block, index) => {
      if (block.type === "text") {
        const text = str(block.text);
        if (!text) return;
        items.push({
          kind: "text",
          id: `text:${row.id}:${index}`,
          runId,
          messageId: row.id,
          step: 0,
          text,
          done: true,
        });
      } else if (block.type === "tool_use") {
        const useId = str(block.id);
        // 프로바이더가 같은 id를 다시 쓰더라도 카드가 겹치지 않게 한다.
        const id = toolCalls[useId] ? `${useId}@${row.id}` : useId;
        toolCalls[id] = restoredTool(block, results.get(useId), runId, id);
        items.push({ kind: "tool", id: `tool:${id}`, toolCallId: id });
      }
    });
    files = mergeFiles(
      files,
      row.files.map((file) => changedFileOf(file, runId)),
    );
  }

  const base: Partial<RunViewState> = {
    sessionId: detail?.id ?? null,
    items,
    toolCalls,
    files,
    sessionAutoApprove: detail?.approval_override === "off",
  };
  if (active) return { ...base, ...activeRunState(active) };
  const last = detail?.last_run ?? null;
  if (!last) return base;
  const finished = finishedRunState(last);
  if (last.status === "failed" && finished.error) {
    items.push({ kind: "error", id: `error:${last.id}`, ...finished.error });
  } else if (last.status === "cancelled") {
    const timeout = last.error?.code === "input_timeout";
    items.push({
      kind: "notice",
      id: `notice:${last.id}:cancelled`,
      code: timeout ? "input_timeout" : "stopped",
      message: timeout ? (last.error?.message ?? null) : null,
      count: null,
    });
  }
  return { ...base, ...finished };
}
