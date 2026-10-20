import { describe, expect, it, vi } from "vitest";
import type { DataRunEvent, WfEvent } from "@/api/run-events";
import { ApiError } from "@/api/http";
import type { WfConversationDetail, WfMessageList } from "@/api/types";
import { chatContract } from "@/test/chatContract";
import { sseBody } from "@/test/contract";
import { ChatflowSession, type ChatflowApi } from "./chatflowSession";
import { WfRunController, type WfRunApi } from "./wfRunController";

const { basic, approval } = chatContract;

function sse(events: readonly WfEvent[], runId: string): Response {
  return new Response(sseBody(events as unknown as DataRunEvent[]), {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "X-Run-Id": runId },
  });
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Server {
  detail: WfConversationDetail;
  messages: WfMessageList;
  // 요청 경로(쿼리 포함) → 응답
  responses: Record<string, () => Response>;
}

function setup(server: Server, options: { conversationId?: string } = {}) {
  const requests: string[] = [];
  const runApi: WfRunApi = {
    eventsRequest: (runId, after) => new Request(`http://test/events/${runId}?after=${after}`),
    stop: vi.fn(async () => ({})),
    input: vi.fn(async () => ({})),
    fetch: vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const key = `${request.method} ${url.pathname}${url.search}`;
      requests.push(key);
      const respond = server.responses[key];
      if (!respond) throw new Error(`예상하지 못한 요청 ${key}`);
      return respond();
    }),
  };
  const api: ChatflowApi = {
    getConversation: vi.fn(async () => server.detail),
    listMessages: vi.fn(async () => server.messages),
    messageRequest: (conversationId, body) =>
      new Request(`http://test/conversations/${conversationId}/messages`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    upload: vi.fn(),
    stop: vi.fn(async () => ({})),
    fileUrl: (conversationId, fileId) => `http://test/conversations/${conversationId}/${fileId}`,
  };
  const runs = new WfRunController(runApi, { sleep: async () => undefined });
  const onSettled = vi.fn();
  const onSent = vi.fn();
  const session = new ChatflowSession(api, runs, {
    createConversation: vi.fn(async () => options.conversationId ?? "c-new"),
    onSent,
    onSettled,
  });
  return { session, runs, api, runApi, requests, onSettled, onSent };
}

const settled = (runs: WfRunController) => vi.waitFor(() => expect(runs.streaming).toBe(false));

describe("ChatflowSession", () => {
  it("첫 메시지로 대화를 만들고 턴을 따라가며, 끝나면 기록을 다시 읽는다", async () => {
    const [turn] = basic.turns;
    const runId = turn!.headers["x-run-id"]!;
    const conversationId = basic.conversationId;
    const server: Server = {
      detail: { ...basic.detail, active_run: null },
      messages: { items: basic.messages.items.slice(0, 2), has_more: false },
      responses: {
        [`POST /conversations/${conversationId}/messages`]: () => sse(turn!.events, runId),
      },
    };
    const { session, runs, api, onSettled, onSent } = setup(server, { conversationId });

    const result = await session.send("첫 질문", [], { q: "x" });

    expect(result).toBe(true);
    expect(session.conversationId).toBe(conversationId);
    // 첫 메시지로 대화 제목이 생겨 목록을 다시 읽게 한다.
    expect(onSent).toHaveBeenCalledWith(conversationId);
    expect(session.meta.getState().turn).toMatchObject({ runId, text: "첫 질문" });
    await settled(runs);
    await vi.waitFor(() => expect(onSettled).toHaveBeenCalledWith(conversationId));
    expect(api.listMessages).toHaveBeenCalledTimes(1);
    const chat = session.chat.getState();
    expect(chat.items.map((item) => item.id)).toEqual([`user:${runId}`, `text:${runId}`]);
    expect(chat).toMatchObject({ openRunId: null, liveRunId: runId, started: true });
    // 1단계 채팅 목록이 읽는 스토어에도 같은 목록이 있다.
    expect(session.view.getState().items).toBe(chat.items);
  });

  it("진행 중인 런이 있어 409를 받으면 대화를 다시 열어 그 런에 붙고 승인 카드를 보인다", async () => {
    const { conversationId, runId } = approval;
    const server: Server = {
      detail: approval.waitingDetail,
      messages: approval.waitingMessages,
      responses: {
        [`POST /conversations/${conversationId}/messages`]: () =>
          json(approval.conflict.status, approval.conflict.body),
        [`GET /events/${runId}?after=0`]: () => sse(approval.replayWaiting, runId),
      },
    };
    const { session, runs, requests } = setup(server, { conversationId });

    const result = await session.send("또 보냄", [], {});

    expect(result).toBeInstanceOf(ApiError);
    expect((result as ApiError).code).toBe("run_in_progress");
    await vi.waitFor(() => expect(session.chat.getState().pending).not.toBeNull());
    await settled(runs);
    expect(requests).toContain(`GET /events/${runId}?after=0`);
    const chat = session.chat.getState();
    expect(chat).toMatchObject({ openRunId: runId, status: "waiting_input" });
    expect(chat.items.map((item) => (item.kind === "user" ? item.text : item.kind))).toEqual([
      "셸을 써 줘",
      "text",
    ]);
    // 보내려던 글은 목록에 남지 않는다(입력창이 되돌려 놓는다).
    expect(session.meta.getState().turn).toBeNull();
  });

  it("새로 고친 뒤 대화를 열면 기다리는 런에 다시 붙고, 승인하면 이어지는 답을 한 벌로 받는다", async () => {
    const { conversationId, runId } = approval;
    const last = approval.events[approval.events.length - 1]!.seq;
    const server: Server = {
      detail: approval.waitingDetail,
      messages: approval.waitingMessages,
      responses: {
        [`GET /events/${runId}?after=0`]: () => sse(approval.replayWaiting, runId),
        [`GET /events/${runId}?after=${last}`]: () => sse(approval.resume, runId),
      },
    };
    const { session, runs, runApi, onSettled } = setup(server);

    await session.open(conversationId);
    await settled(runs);
    expect(session.chat.getState().pending).toMatchObject({ kind: "approval", runId });

    server.detail = approval.doneDetail;
    server.messages = approval.doneMessages;
    const ok = await session.respond({ decision: "approve", session_auto_approve: false });

    expect(ok).toBe(true);
    expect(runApi.input).toHaveBeenCalledWith(runId, {
      decision: "approve",
      session_auto_approve: false,
    });
    await settled(runs);
    await vi.waitFor(() => expect(onSettled).toHaveBeenCalled());
    const chat = session.chat.getState();
    expect(chat.pending).toBeNull();
    expect(chat.status).toBe("idle");
    const answer = chat.items.find((item) => item.kind === "text");
    expect(answer).toMatchObject({ text: approval.doneMessages.items[1]!.text, done: true });
  });

  it("지워진 대화를 열면 새 대화로 돌아간다", async () => {
    const server: Server = {
      detail: basic.detail,
      messages: basic.messages,
      responses: {},
    };
    const { session, api } = setup(server);
    const onConversationChange = vi.fn();
    session.configure({ onConversationChange });
    vi.mocked(api.getConversation).mockRejectedValueOnce(
      new ApiError(404, "conversation_not_found", "없다"),
    );

    await session.open("gone");

    expect(session.conversationId).toBeNull();
    expect(onConversationChange).toHaveBeenCalledWith(null);
    expect(session.meta.getState()).toMatchObject({ loading: false, loadError: null });
  });
});
