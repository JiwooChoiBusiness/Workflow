import { describe, expect, it } from "vitest";
import type { WfEvent } from "@/api/run-events";
import type { WfMessageOut } from "@/api/types";
import { chatContract } from "@/test/chatContract";
import { secondNodesContract } from "@/test/secondNodesContract";
import type { TimelineItem } from "@/stores/runStore";
import {
  chatView,
  EMPTY_HISTORY,
  historyOf,
  mergeAnswer,
  reuseItems,
  type ChatHistory,
} from "./chatAdapter";
import { applyWfEvents, initialWfRunState } from "./wfRunStore";

const { basic, approval } = chatContract;
const IDLE = initialWfRunState();

function live(events: readonly WfEvent[]) {
  return applyWfEvents(initialWfRunState(), events);
}

// 서버가 링버퍼를 버린 뒤의 재생(영속 이벤트만)
function persisted(events: readonly WfEvent[]): WfEvent[] {
  const transient = new Set(["answer.delta", "node.text_delta", "node.progress", "node.waiting"]);
  return events.filter((event) => !transient.has(event.type));
}

function upTo(events: readonly WfEvent[], type: string, nodeId?: string): WfEvent[] {
  const index = events.findIndex(
    (event) =>
      event.type === type &&
      (nodeId === undefined || (event.data as { node_id?: string }).node_id === nodeId),
  );
  if (index < 0) throw new Error(`${type} 이벤트가 없다`);
  return events.slice(0, index + 1);
}

function texts(items: readonly TimelineItem[]) {
  return items.map((item) =>
    item.kind === "user" || item.kind === "text"
      ? `${item.kind}:${item.text}`
      : `${item.kind}:${item.id}`,
  );
}

function message(partial: Partial<WfMessageOut> & Pick<WfMessageOut, "id" | "role">): WfMessageOut {
  return {
    conversation_id: "c1",
    run_id: null,
    seq: 1,
    text: "",
    inputs: null,
    files: [],
    usage: null,
    status: null,
    created_at: "2026-10-14T00:00:00Z",
    ...partial,
  };
}

describe("chatAdapter: 서버 기록", () => {
  it("두 턴이 user·답 순서로 보이고 끝난 대화라 열린 런이 없다", () => {
    const view = chatView(historyOf(basic.messages, basic.detail), null, IDLE);
    expect(texts(view.items)).toEqual([
      "user:첫 질문",
      "text:가짜 응답: 첫 질문",
      "user:둘째 질문",
      "text:가짜 응답: 둘째 질문",
    ]);
    expect(view.items.every((item) => item.kind !== "text" || item.done)).toBe(true);
    expect(view).toMatchObject({ openRunId: null, status: "idle", pending: null, started: true });
    expect(view.liveRunId).toBeNull();
  });

  it("시작 문구는 맨 앞에, 서버가 앞 메시지를 잘랐으면 그 안내를 둔다", () => {
    const history: ChatHistory = { ...historyOf(basic.messages, basic.detail), hasMore: true };
    const view = chatView(history, null, IDLE, { opening: "  무엇을 도와드릴까요?  " });
    expect(view.items[0]).toMatchObject({ kind: "text", id: "opening", done: true });
    expect(view.items[1]).toMatchObject({ kind: "notice", code: "history_truncated" });
  });

  it("실패한 턴은 오류를(마지막 런이면 그 이유를), 중지한 턴은 중지 알림을 남긴다", () => {
    const history: ChatHistory = {
      messages: [
        message({ id: "u1", role: "user", run_id: "r1", text: "하나" }),
        message({ id: "a1", role: "assistant", run_id: "r1", text: "앞부분", status: "failed" }),
        message({ id: "u2", role: "user", run_id: "r2", text: "둘" }),
        message({ id: "a2", role: "assistant", run_id: "r2", status: "cancelled" }),
        message({ id: "u3", role: "user", run_id: "r3", text: "셋" }),
        message({ id: "a3", role: "assistant", run_id: "r3", status: "failed" }),
      ],
      hasMore: false,
      lastRun: {
        ...basic.detail.last_run!,
        id: "r3",
        status: "failed",
        error: { code: "timeout", message: "런 시간을 넘었다", retryable: false },
      },
    };
    const view = chatView(history, null, IDLE);
    expect(texts(view.items)).toEqual([
      "user:하나",
      "text:앞부분",
      "error:error:r1",
      "user:둘",
      "notice:notice:r2:cancelled",
      "user:셋",
      "error:error:r3",
    ]);
    expect(view.items[2]).toMatchObject({ code: "", message: "" });
    expect(view.items[6]).toMatchObject({ code: "timeout", message: "런 시간을 넘었다" });
  });
});

describe("chatAdapter: 진행 중인 턴", () => {
  const [first] = basic.turns;
  const runId = first!.headers["x-run-id"]!;

  it("보낸 직후에는 보낸 글만, 런 id를 받으면 답 자리가 생겨 델타대로 자란다", () => {
    const turn = { runId: null, text: "첫 질문", files: [] };
    expect(texts(chatView(EMPTY_HISTORY, turn, IDLE).items)).toEqual(["user:첫 질문"]);

    const started = { ...turn, runId };
    const queued = chatView(EMPTY_HISTORY, started, initialWfRunState({ runId }));
    expect(queued.items[1]).toMatchObject({ kind: "text", text: "", done: false });
    expect(queued).toMatchObject({ openRunId: runId, status: "queued", liveRunId: runId });

    const streaming = chatView(EMPTY_HISTORY, started, live(upTo(first!.events, "answer.delta")));
    expect(streaming.items[1]).toMatchObject({ text: "가짜 응답: 첫 질문", done: false });
    expect(streaming.status).toBe("running");

    const done = chatView(EMPTY_HISTORY, started, live(first!.events));
    expect(done.items[1]).toMatchObject({ text: "가짜 응답: 첫 질문", done: true });
    expect(done).toMatchObject({ openRunId: null, status: "idle", started: true });
  });

  it("목록을 다시 읽으면 서버 행으로 그리고 같은 턴을 두 번 그리지 않는다", () => {
    const turn = { runId, text: "첫 질문", files: [] };
    const view = chatView(historyOf(basic.messages, basic.detail), turn, live(first!.events));
    expect(texts(view.items)).toEqual([
      "user:첫 질문",
      "text:가짜 응답: 첫 질문",
      "user:둘째 질문",
      "text:가짜 응답: 둘째 질문",
    ]);
    // 런 id로 키를 잡아 보낸 직후의 항목과 서버 행의 항목이 같은 자리다.
    expect(view.items[0]!.id).toBe(`user:${runId}`);
  });

  it("다른 대화의 런 상태는 쓰지 않는다", () => {
    const view = chatView(EMPTY_HISTORY, null, live(first!.events));
    expect(view.items).toEqual([]);
    expect(view.liveRunId).toBeNull();
  });
});

describe("chatAdapter: 승인 대기와 재개", () => {
  const { runId } = approval;
  const waitingHistory = historyOf(approval.waitingMessages, approval.waitingDetail);
  const prefix = approval.waitingMessages.items[1]!.text;
  const full = approval.doneMessages.items[1]!.text;

  it("기다리는 동안 멈춘 데까지의 답과 승인 카드가 보이고 보내기 대신 중지를 둔다", () => {
    const view = chatView(waitingHistory, null, live(approval.events));
    expect(texts(view.items)).toEqual(["user:셸을 써 줘", `text:${prefix}`]);
    expect(view.items[1]).toMatchObject({ done: true });
    expect(view.pending).toMatchObject({ kind: "approval", runId });
    expect(view.pendingNodeId).toBe("agent_1");
    expect(view).toMatchObject({ openRunId: runId, status: "waiting_input" });
  });

  it("human_input 폼을 기다리면 승인 카드 대신 폼을 내놓는다", () => {
    const formWait = secondNodesContract.events.find(
      (event) => event.type === "workflow.waiting_input",
    )!;
    const events = approval.events.map((event) =>
      event.type === "workflow.waiting_input" ? { ...event, data: formWait.data } : event,
    ) as WfEvent[];
    const view = chatView(waitingHistory, null, live(events));
    expect(view.pending).toBeNull();
    expect(view.form?.kind).toBe("form");
    expect(view.form?.form.fields.map((field) => field.name)).toEqual(["ok", "note"]);
    expect(view.pendingNodeId).toBe("ask");
    expect(view).toMatchObject({ openRunId: runId, status: "waiting_input" });
  });

  it("재개 스트림은 앞부분을 다시 보내지 않아도 한 벌로 이어 붙인다", () => {
    const state = live([...approval.events, ...upTo(approval.resume, "answer.delta", "answer_2")]);
    expect(state.answerResumedAt).toBe(prefix.length);
    const view = chatView(waitingHistory, null, state);
    expect(view.items[1]).toMatchObject({ kind: "text", text: full, done: false });
    expect(view.pending).toBeNull();

    const done = chatView(waitingHistory, null, live([...approval.events, ...approval.resume]));
    expect(done.items[1]).toMatchObject({ text: full, done: true });
    expect(done.status).toBe("idle");
  });

  it("새로 고친 뒤 델타 없이 다시 붙어도 앞부분은 저장된 답 글로 채운다", () => {
    const replay = live(persisted(approval.replayWaiting));
    expect(replay.answerText).toBe("");
    const waiting = chatView(waitingHistory, null, replay);
    expect(waiting.items[1]).toMatchObject({ text: prefix });
    expect(waiting.pending).not.toBeNull();

    const resumed = applyWfEvents(replay, upTo(approval.resume, "answer.delta", "answer_2"));
    expect(chatView(waitingHistory, null, resumed).items[1]).toMatchObject({ text: full });
  });

  it("런 전체의 답(answer.completed)을 받으면 그 글이 이긴다", () => {
    expect(mergeAnswer("앞", { answerText: "앞뒤", answerResumedAt: null })).toBe("앞뒤");
    expect(mergeAnswer("앞부분", { answerText: "", answerResumedAt: null })).toBe("앞부분");
    expect(mergeAnswer("앞부분", { answerText: "뒤", answerResumedAt: 0 })).toBe("앞부분뒤");
    expect(mergeAnswer("앞", { answerText: "앞부분뒤", answerResumedAt: 3 })).toBe("앞부분뒤");
  });
});

describe("reuseItems", () => {
  it("내용이 같은 항목은 앞 객체를 쓰고, 하나도 안 바뀌면 앞 목록을 그대로 준다", () => {
    const first = chatView(historyOf(basic.messages, basic.detail), null, IDLE).items;
    const again = chatView(historyOf(basic.messages, basic.detail), null, IDLE).items;
    expect(reuseItems(first, again)).toBe(first);

    const changed = again.map((item) =>
      item.kind === "text" && item.text.includes("둘째") ? { ...item, text: "바뀜" } : item,
    );
    const merged = reuseItems(first, changed);
    expect(merged).not.toBe(first);
    expect(merged[0]).toBe(first[0]);
    expect(merged[3]).toMatchObject({ text: "바뀜" });
  });
});
