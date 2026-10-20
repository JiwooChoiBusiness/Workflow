/*
 * 챗플로우 대화 화면의 메시지 목록(설계 7.4). 서버에서 읽은 대화 메시지(GET .../messages)와 지금 따라가는
 * 턴의 런 상태(wfRunStore)를 1단계 채팅 목록(MessageList)이 그리는 모양 하나로 합친다. 콘솔 미리보기와
 * 웹앱이 같이 쓴다. 한 턴은 user·assistant 두 행이고 둘 다 그 턴의 런 id를 가지므로 런 id로 맞춘다.
 */
import type { WfConversationDetail, WfFileOut, WfMessageList, WfMessageOut } from "@/api/types";
import type {
  AttachmentView,
  PendingInteraction,
  RunStatus,
  TimelineItem,
} from "@/stores/runStore";
import type { WfPendingForm, WfRunViewState } from "./wfRunStore";

export interface ChatHistory {
  // seq 오름차순
  messages: readonly WfMessageOut[];
  // 서버가 마지막 100개만 줬다. 더 앞은 로그 화면에서 본다.
  hasMore: boolean;
  // 대화 상세의 마지막 런. 메시지에는 상태만 있어 실패 이유는 여기서 읽는다.
  lastRun: WfConversationDetail["last_run"];
}

export const EMPTY_HISTORY: ChatHistory = { messages: [], hasMore: false, lastRun: null };

export function historyOf(list: WfMessageList, detail: WfConversationDetail | null): ChatHistory {
  return { messages: list.items, hasMore: list.has_more, lastRun: detail?.last_run ?? null };
}

// 이 화면에서 보낸 턴. 서버 목록이 아직 그 턴의 행을 모를 때 user 메시지를 먼저 보여 준다.
export interface ChatTurn {
  // 응답 헤더(X-Run-Id)를 받기 전에는 null
  runId: string | null;
  text: string;
  files: AttachmentView[];
}

export type LiveRun = Pick<
  WfRunViewState,
  "runId" | "status" | "answerText" | "answerResumedAt" | "pendingInteraction" | "error"
>;

export interface ChatView {
  items: TimelineItem[];
  // 1단계 승인·질문 카드가 받는 모양(런 id 포함). 이 대화의 런이 기다릴 때만 있다.
  pending: PendingInteraction | null;
  // human_input 노드의 폼(M19). 1단계 카드 모양이 아니라 따로 둔다.
  form: WfPendingForm | null;
  pendingNodeId: string | null;
  // 끝나지 않은 턴의 런과 그 상태. 있으면 보내기 대신 중지를 둔다.
  openRunId: string | null;
  status: RunStatus;
  // wfRunStore가 이 대화의 런을 따라가고 있으면 그 런 id(상태 줄·노드 타임라인을 이 대화 것으로 본다)
  liveRunId: string | null;
  // 보낸 메시지가 있다. 입력 폼은 첫 메시지 전에만 연다.
  started: boolean;
}

export const EMPTY_VIEW: ChatView = {
  items: [],
  pending: null,
  form: null,
  pendingNodeId: null,
  openRunId: null,
  status: "idle",
  liveRunId: null,
  started: false,
};

const OPEN: ReadonlySet<RunStatus> = new Set(["queued", "running", "waiting_input"]);
const ACTIVE: ReadonlySet<RunStatus> = new Set(["queued", "running"]);

// 메시지에 붙인 업로드(wf_file). 대화의 파일 경로(ChatflowApi.fileUrl)로 내려받는다. 서버가 메시지를
// 받기 전의 보내는 중인 턴은 아직 대화에 붙지 않아 이름과 크기만 보인다.
export function attachmentsOf(files: readonly WfFileOut[], downloadable = true): AttachmentView[] {
  return files.map((file) => ({
    sessionFileId: file.id,
    name: file.filename,
    size: file.size_bytes,
    downloadable,
  }));
}

/*
 * 저장된 답 글(stored)과 스트림으로 받은 글을 합친다. 재개 스트림은 멈추기 전에 흘린 앞부분을 다시 보내지
 * 않고, 새로 고친 뒤 다시 붙으면 링버퍼가 버려진 앞부분의 델타도 오지 않는다. 그 앞부분은 서버가 런이
 * 멈출 때 메시지에 써 둔 글이 더 길거나 같다. answer.completed를 받았으면 그 글이 런 전체의 답이다.
 */
export function mergeAnswer(
  stored: string,
  live: Pick<LiveRun, "answerText" | "answerResumedAt">,
): string {
  const { answerText, answerResumedAt } = live;
  if (answerResumedAt === null) return answerText.length >= stored.length ? answerText : stored;
  const before = answerText.slice(0, answerResumedAt);
  return (before.length >= stored.length ? before : stored) + answerText.slice(answerResumedAt);
}

function userItem(key: string, text: string, files: AttachmentView[]): TimelineItem {
  return files.length > 0
    ? { kind: "user", id: `user:${key}`, text, files }
    : { kind: "user", id: `user:${key}`, text };
}

export function chatView(
  history: ChatHistory,
  turn: ChatTurn | null,
  live: LiveRun,
  options: { opening?: string | null } = {},
): ChatView {
  const items: TimelineItem[] = [];
  const opening = options.opening?.trim();
  if (opening) {
    items.push({
      kind: "text",
      id: "opening",
      runId: "",
      messageId: "",
      step: 0,
      text: opening,
      done: true,
    });
  }
  if (history.hasMore) {
    items.push({
      kind: "notice",
      id: "notice:history_truncated",
      code: "history_truncated",
      message: null,
      count: null,
    });
  }

  const known = new Set<string>();
  for (const message of history.messages) if (message.run_id) known.add(message.run_id);
  const liveRunId =
    live.runId !== null && (known.has(live.runId) || turn?.runId === live.runId)
      ? live.runId
      : null;
  let openRunId: string | null = null;
  let status: RunStatus = "idle";
  let started = false;

  const assistant = (
    key: string,
    runId: string | null,
    messageId: string,
    stored: string,
    storedStatus: RunStatus | null,
  ) => {
    const tracked = runId !== null && runId === liveRunId;
    // reset({runId}) 직후(첫 이벤트 전)는 idle이다. 큐에 들어간 것으로 본다.
    const current: RunStatus = tracked
      ? live.status === "idle"
        ? "queued"
        : live.status
      : (storedStatus ?? "queued");
    const text = tracked ? mergeAnswer(stored, live) : stored;
    if (runId !== null && OPEN.has(current)) {
      openRunId = runId;
      status = current;
    }
    const streaming = ACTIVE.has(current);
    if (text || streaming) {
      items.push({
        kind: "text",
        id: `text:${key}`,
        runId: runId ?? "",
        messageId,
        step: 0,
        text,
        done: !streaming,
      });
    }
    if (current === "failed") {
      const error =
        tracked && live.error
          ? live.error
          : history.lastRun?.id === runId && history.lastRun.error
            ? history.lastRun.error
            : null;
      items.push({
        kind: "error",
        id: `error:${key}`,
        code: error?.code ?? "",
        message: error?.message ?? "",
        retryable: error?.retryable ?? false,
      });
    } else if (current === "cancelled") {
      items.push({
        kind: "notice",
        id: `notice:${key}:cancelled`,
        code: "stopped",
        message: null,
        count: null,
      });
    }
  };

  for (const message of history.messages) {
    const key = message.run_id ?? message.id;
    if (message.role === "user") {
      started = true;
      items.push(userItem(key, message.text, attachmentsOf(message.files)));
    } else {
      assistant(key, message.run_id, message.id, message.text, message.status);
    }
  }
  if (turn && !(turn.runId !== null && known.has(turn.runId))) {
    started = true;
    const key = turn.runId ?? "pending";
    items.push(userItem(key, turn.text, turn.files));
    if (turn.runId !== null) assistant(key, turn.runId, "", "", null);
  }

  const waiting = live.status === "waiting_input" ? live.pendingInteraction : null;
  let pending: PendingInteraction | null = null;
  let form: WfPendingForm | null = null;
  if (liveRunId !== null && waiting) {
    if (waiting.kind === "form") form = waiting;
    else pending = { ...waiting.interaction, runId: liveRunId };
  }
  return {
    items,
    pending,
    form,
    pendingNodeId: pending || form ? (waiting?.nodeId ?? null) : null,
    openRunId,
    status,
    liveRunId,
    started,
  };
}

function sameItem(a: TimelineItem, b: TimelineItem): boolean {
  const left = a as unknown as Record<string, unknown>;
  const right = b as unknown as Record<string, unknown>;
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) return false;
  return keys.every(
    (key) =>
      left[key] === right[key] ||
      (key === "files" && JSON.stringify(left[key]) === JSON.stringify(right[key])),
  );
}

/*
 * 새로 만든 목록에서 내용이 같은 항목은 앞 목록의 객체를 그대로 쓴다. 델타가 올 때마다 목록을 다시 만들어도
 * 메시지 항목(memo)이 다시 그려지지 않게 한다. 하나도 바뀌지 않았으면 앞 목록을 돌려준다.
 */
export function reuseItems(
  previous: readonly TimelineItem[],
  next: readonly TimelineItem[],
): TimelineItem[] {
  const byId = new Map(previous.map((item) => [item.id, item]));
  let same = previous.length === next.length;
  const merged = next.map((item, index) => {
    const old = byId.get(item.id);
    const kept = old && sameItem(old, item) ? old : item;
    if (kept !== previous[index]) same = false;
    return kept;
  });
  return same ? (previous as TimelineItem[]) : merged;
}
