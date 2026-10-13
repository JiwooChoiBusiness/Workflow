import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import type { DataRunEvent } from "@/api/run-events";
import { contract, sseBody } from "@/test/contract";
import type { TimelineItem } from "@/stores/runStore";
import { ChatSessionController, type ChatSessionApi } from "./chatSession";
import type { MessageList, SessionDetail } from "./types";

const BASE = "http://test";

function sseResponse(events: readonly DataRunEvent[], options: { failAfter?: boolean } = {}) {
  const encoder = new TextEncoder();
  let sent = false;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (!sent) {
        sent = true;
        controller.enqueue(encoder.encode(sseBody(events)));
      } else if (options.failAfter) {
        controller.error(new TypeError("network error"));
      } else {
        controller.close();
      }
    },
  });
  const first = events[0];
  return new Response(body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      ...(first ? { "X-Run-Id": first.run_id, "X-Session-Id": first.session_id } : {}),
    },
  });
}

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Route {
  method: string;
  path: string;
  query: Record<string, string>;
}

function setup(
  options: {
    detail?: SessionDetail;
    messages?: MessageList;
    respond?: (route: Route) => Response;
  } = {},
) {
  const requests: Route[] = [];
  const calls: string[] = [];
  const api: ChatSessionApi = {
    getSession: vi.fn(async () => {
      calls.push("getSession");
      if (!options.detail) throw new ApiError(404, "not_found", "세션을 찾을 수 없다");
      return options.detail;
    }),
    listMessages: vi.fn(async () => {
      calls.push("listMessages");
      return options.messages ?? { items: [], has_more: false };
    }),
    upload: vi.fn(async () => contract.question.upload),
    stop: vi.fn(async () => contract.stopWaiting.stop),
    input: vi.fn(async () => contract.approval.input),
    patchSession: vi.fn(async () => contract.autoApprove.patchOff),
    messageRequest: (sessionId, body) =>
      new Request(`${BASE}/api/console/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    eventsRequest: (sessionId, after, runId) =>
      new Request(
        `${BASE}/api/console/sessions/${sessionId}/events?after=${after}${runId ? `&run_id=${runId}` : ""}`,
      ),
    fetch: vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const route = {
        method: request.method,
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
      };
      requests.push(route);
      return options.respond?.(route) ?? jsonResponse(500, {});
    }),
  };
  const onSessionChange = vi.fn();
  const onError = vi.fn();
  const onSettled = vi.fn();
  const controller = new ChatSessionController(api, {
    createSession: vi.fn(async () => "s-new"),
    onSessionChange,
    onError,
    onSettled,
    sleep: async () => undefined,
  });
  const state = () => controller.store.getState();
  return { controller, api, requests, calls, onSessionChange, onError, onSettled, state };
}

function kinds(items: TimelineItem[]) {
  return items.map((item) => item.kind);
}

const approval = contract.approval;
const runId = approval.send.runId;
const sessionId = approval.send.sessionId;

describe("ChatSessionController: 새로 고침 복구", () => {
  it("세션을 열면 상세→메시지 순으로 읽고 진행 중인 런을 after=0으로 다시 구독해 승인 카드를 되살린다", async () => {
    const t = setup({
      detail: approval.detailWaiting,
      messages: approval.messagesWaiting,
      respond: (route) =>
        route.path.endsWith("/events") ? sseResponse(approval.send.events) : jsonResponse(500, {}),
    });
    await t.controller.open(sessionId);
    expect(t.calls).toEqual(["getSession", "listMessages"]);
    await vi.waitFor(() => expect(t.onSettled).toHaveBeenCalledWith(sessionId));
    expect(t.requests).toEqual([
      {
        method: "GET",
        path: `/api/console/sessions/${sessionId}/events`,
        query: { after: "0", run_id: runId },
      },
    ]);
    const s = t.state();
    expect(s.status).toBe("waiting_input");
    expect(kinds(s.items)).toEqual(["user", "tool"]);
    const tool = Object.values(s.toolCalls)[0];
    expect(tool).toMatchObject({ name: "shell", phase: "awaiting_approval" });
    expect(s.pendingInteraction).toMatchObject({ kind: "approval", runId, tool_call_id: tool.id });
    expect(t.controller.meta.getState()).toMatchObject({ loading: false, connection: null });
  });

  it("지워진 세션이면 잊고 빈 대화로 시작한다", async () => {
    const t = setup();
    await t.controller.open("gone");
    expect(t.onSessionChange).toHaveBeenCalledWith(null);
    expect(t.controller.meta.getState()).toMatchObject({ sessionId: null, loadError: null });
    expect(t.state().items).toEqual([]);
  });

  it("기록을 불러오지 못하면 오류를 남긴다", async () => {
    const t = setup();
    vi.mocked(t.api.getSession).mockRejectedValueOnce(new ApiError(0, "network_error", "끊김"));
    await t.controller.open(sessionId);
    expect(t.controller.meta.getState().loadError).toBeInstanceOf(ApiError);
    expect(t.controller.meta.getState().loading).toBe(false);
  });
});

describe("ChatSessionController: 승인·질문·중지", () => {
  async function openWaiting(respond: (route: Route) => Response) {
    let first = true;
    const t = setup({
      detail: approval.detailWaiting,
      messages: approval.messagesWaiting,
      respond: (route) => {
        if (first && route.path.endsWith("/events")) {
          first = false;
          return sseResponse(approval.send.events);
        }
        return respond(route);
      },
    });
    await t.controller.open(sessionId);
    await vi.waitFor(() => expect(t.state().status).toBe("waiting_input"));
    return t;
  }

  it("승인하면 202 뒤 마지막 seq 다음부터 다시 받아 끝까지 그린다", async () => {
    const t = await openWaiting((route) =>
      route.path.endsWith("/events") ? sseResponse(approval.resume) : jsonResponse(500, {}),
    );
    await t.controller.respond(runId, { decision: "approve", session_auto_approve: false });
    expect(t.api.input).toHaveBeenCalledWith(runId, {
      decision: "approve",
      session_auto_approve: false,
    });
    expect(t.requests.at(-1)?.query).toEqual({ after: "10", run_id: runId });
    await vi.waitFor(() => expect(t.state().status).toBe("succeeded"));
    const s = t.state();
    expect(s.pendingInteraction).toBeNull();
    expect(kinds(s.items)).toEqual(["user", "tool", "interaction", "text"]);
    const tool = Object.values(s.toolCalls)[0];
    expect(tool).toMatchObject({ phase: "done", result: "ok", exitCode: 0 });
    expect(tool.output[0].text).toBe("fake-llm\n");
    expect(s.items.at(-1)).toMatchObject({ text: "도구 실행 결과를 확인했습니다.", done: true });
  });

  it("승인이 이미 처리되었으면(409) 알리고 서버 상태로 다시 맞춘다", async () => {
    const t = await openWaiting(() => sseResponse(approval.send.events));
    vi.mocked(t.api.input).mockRejectedValueOnce(
      new ApiError(409, "run_not_waiting", "답이나 승인을 기다리는 런이 아니다"),
    );
    await t.controller.respond(runId, { decision: "reject", note: "안 돼" });
    expect(t.api.input).toHaveBeenCalledWith(runId, { decision: "reject", note: "안 돼" });
    expect(t.onError).toHaveBeenCalledWith(expect.objectContaining({ code: "run_not_waiting" }));
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "getSession")).toHaveLength(2));
  });

  it("입력을 기다리는 런을 중지하면 다시 붙어 run.completed(cancelled)를 받는다", async () => {
    const stopped = contract.stopWaiting;
    const stoppedRun = stopped.send[0].run_id;
    const t = setup({
      detail: {
        ...approval.detailWaiting,
        active_run: { ...approval.detailWaiting.active_run!, id: stoppedRun },
      },
      messages: { items: [], has_more: false },
      respond: (route) =>
        route.query.after === "0" ? sseResponse(stopped.send) : sseResponse(stopped.after),
    });
    await t.controller.open(sessionId);
    await vi.waitFor(() => expect(t.state().status).toBe("waiting_input"));
    expect(t.requests[0].query).toEqual({ after: "0", run_id: stoppedRun });
    await t.controller.stop();
    expect(t.api.stop).toHaveBeenCalledWith(stoppedRun);
    await vi.waitFor(() => expect(t.state().status).toBe("cancelled"));
    expect(t.requests.at(-1)?.query).toEqual({ after: "10", run_id: stoppedRun });
    const s = t.state();
    expect(s.pendingInteraction).toBeNull();
    expect(Object.values(s.toolCalls)[0]).toMatchObject({ phase: "done", result: "cancelled" });
    expect(s.items.at(-1)).toMatchObject({ kind: "notice", code: "stopped" });
  });

  it("질문에는 /input {answer}로 답하고 바뀐 파일을 모은다", async () => {
    const q = contract.question;
    const qRun = q.send[0].run_id;
    const t = setup({
      detail: q.detail,
      messages: q.messagesWaiting,
      respond: (route) => (route.query.after === "0" ? sseResponse(q.send) : sseResponse(q.answer)),
    });
    await t.controller.open(q.detail.id);
    await vi.waitFor(() => expect(t.state().pendingInteraction?.kind).toBe("question"));
    expect(t.state().pendingInteraction).toMatchObject({
      question: "어떤 형식으로 저장할까요?",
      options: ["CSV", "JSON"],
    });
    await t.controller.respond(qRun, { answer: "CSV" });
    expect(t.api.input).toHaveBeenCalledWith(qRun, { answer: "CSV" });
    expect(t.requests.at(-1)?.query).toEqual({ after: "18", run_id: qRun });
    await vi.waitFor(() => expect(t.state().status).toBe("succeeded"));
    const s = t.state();
    expect(s.files.map((file) => [file.path, file.downloadable])).toEqual([
      ["outputs/result.csv", true],
    ]);
    expect(s.items.find((item) => item.kind === "interaction")).toMatchObject({ answer: "CSV" });
  });
});

describe("ChatSessionController: 보내기", () => {
  it("세션이 없으면 만들고, 끊긴 스트림은 X-Run-Id와 마지막 seq로 다시 잇는다", async () => {
    const events = approval.send.events;
    const t = setup({
      respond: (route) => {
        if (route.method === "POST") return sseResponse(events.slice(0, 5), { failAfter: true });
        return sseResponse(events.slice(5));
      },
    });
    await t.controller.open(null);
    const accepted = await t.controller.send("파일 목록 보여줘");
    expect(accepted).toBe(true);
    expect(t.onSessionChange).toHaveBeenCalledWith("s-new");
    await vi.waitFor(() => expect(t.state().status).toBe("waiting_input"));
    expect(t.requests.map((route) => [route.method, route.query])).toEqual([
      ["POST", {}],
      ["GET", { after: "5", run_id: runId }],
    ]);
    expect(kinds(t.state().items)).toEqual(["user", "tool"]);
  });

  it("진행 중인 런이 있어 409를 받으면 false를 돌려주고 보낸 글을 지운 뒤 다시 맞춘다", async () => {
    const t = setup({
      detail: approval.detailDone,
      messages: approval.messagesDone,
      respond: (route) =>
        route.method === "POST"
          ? jsonResponse(contract.conflict.status, contract.conflict.body)
          : jsonResponse(500, {}),
    });
    await t.controller.open(sessionId);
    const before = kinds(t.state().items);
    const accepted = await t.controller.send("겹치는 메시지");
    expect(accepted).toBe(false);
    expect(t.onError).toHaveBeenCalledWith(expect.objectContaining({ code: "run_in_progress" }));
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "getSession")).toHaveLength(2));
    expect(kinds(t.state().items)).toEqual(before);
  });

  it("세션이 지워졌으면(404) 새 세션을 만들어 한 번 다시 보낸다", async () => {
    let posts = 0;
    const t = setup({
      detail: approval.detailDone,
      messages: { items: [], has_more: false },
      respond: (route) => {
        if (route.method !== "POST") return jsonResponse(500, {});
        posts += 1;
        return posts === 1
          ? jsonResponse(404, { error: { code: "not_found", message: "세션을 찾을 수 없다" } })
          : sseResponse(approval.send.events);
      },
    });
    await t.controller.open(sessionId);
    expect(await t.controller.send("다시")).toBe(true);
    expect(t.requests.map((route) => route.path)).toEqual([
      `/api/console/sessions/${sessionId}/messages`,
      "/api/console/sessions/s-new/messages",
    ]);
    expect(t.onError).not.toHaveBeenCalled();
  });

  it("첨부 파일은 먼저 올리고 file_ids로 보낸다", async () => {
    let posted: unknown = null;
    const t = setup({ respond: () => sseResponse(approval.send.events) });
    const original = t.api.messageRequest;
    t.api.messageRequest = (sid, body) => {
      posted = body;
      return original(sid, body);
    };
    await t.controller.open(null);
    const file = new File(["a,b\n1,2\n"], "입력.csv", { type: "text/csv" });
    expect(await t.controller.send("파일 만들고 물어봐", [file])).toBe(true);
    expect(t.api.upload).toHaveBeenCalledWith("s-new", file);
    expect(posted).toEqual({ text: "파일 만들고 물어봐", file_ids: [contract.question.upload.id] });
    expect(t.state().items[0]).toMatchObject({
      kind: "user",
      files: [{ name: "입력.csv", sessionFileId: contract.question.upload.id }],
    });
  });
});

describe("ChatSessionController: 자동 승인과 초기화", () => {
  it("'이 세션에서는 묻지 않기'로 승인하면 칩이 켜지고 끄기는 PATCH approval_override=null이다", async () => {
    const auto = contract.autoApprove;
    const autoRun = auto.send[0].run_id;
    const t = setup({
      respond: (route) =>
        route.method === "POST" ? sseResponse(auto.send) : sseResponse(auto.resume),
    });
    await t.controller.open(null);
    await t.controller.send("한 번 더");
    await vi.waitFor(() => expect(t.state().status).toBe("waiting_input"));
    await t.controller.respond(autoRun, { decision: "approve", session_auto_approve: true });
    expect(t.api.input).toHaveBeenCalledWith(autoRun, {
      decision: "approve",
      session_auto_approve: true,
    });
    await vi.waitFor(() => expect(t.state().status).toBe("succeeded"));
    expect(t.state().sessionAutoApprove).toBe(true);
    // 자동 승인 상태에서 시작한 런은 approval_mode가 off로 온다.
    expect(t.state().approvalMode).toBe("off");
    await t.controller.setAutoApprove(false);
    expect(t.api.patchSession).toHaveBeenCalledWith("s-new", { approval_override: null });
    expect(t.state().sessionAutoApprove).toBe(false);
  });

  it("초기화하면 열린 런을 멈추고 세션을 잊는다", async () => {
    const t = setup({
      detail: approval.detailWaiting,
      messages: approval.messagesWaiting,
      respond: () => sseResponse(approval.send.events),
    });
    await t.controller.open(sessionId);
    await vi.waitFor(() => expect(t.state().status).toBe("waiting_input"));
    await t.controller.reset();
    expect(t.api.stop).toHaveBeenCalledWith(runId);
    expect(t.onSessionChange).toHaveBeenLastCalledWith(null);
    expect(t.state()).toMatchObject({ status: "idle", items: [], runId: null });
  });
});
