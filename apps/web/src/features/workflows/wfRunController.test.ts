import { describe, expect, it, vi } from "vitest";
import type { DataRunEvent, WfEvent } from "@/api/run-events";
import { ApiError } from "@/api/http";
import { sseBody } from "@/test/contract";
import { wfContract } from "@/test/wfContract";
import { WfRunController, type WfRunApi } from "./wfRunController";

function sse(events: readonly WfEvent[], runId: string): Response {
  return new Response(sseBody(events as unknown as DataRunEvent[]), {
    status: 200,
    headers: { "Content-Type": "text/event-stream", "X-Run-Id": runId },
  });
}

function fakeApi(streams: Record<string, WfEvent[]>, runId: string) {
  const requests: string[] = [];
  const api: WfRunApi = {
    eventsRequest: (id, after) => new Request(`http://test/events/${id}?after=${after}`),
    stop: vi.fn(async () => ({})),
    input: vi.fn(async () => ({})),
    fetch: vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      requests.push(`${request.method} ${url.pathname}${url.search}`);
      const events = streams[url.pathname + url.search] ?? [];
      return sse(events, runId);
    }),
  };
  return { api, requests };
}

async function settled(controller: WfRunController) {
  await vi.waitFor(() => expect(controller.streaming).toBe(false));
}

describe("wfRunController", () => {
  it("런을 시작해 끝날 때까지 받고, 헤더의 런 id를 첫 이벤트보다 먼저 알린다", async () => {
    const { runId, events } = wfContract.linear;
    const { api, requests } = fakeApi({ "/start": events }, runId);
    const onSettled = vi.fn();
    const controller = new WfRunController(api, { onSettled, sleep: async () => undefined });

    const accepted = await controller.start(
      () => new Request("http://test/start", { method: "POST" }),
    );

    expect(accepted).toBe(true);
    expect(controller.runId).toBe(runId);
    await settled(controller);
    expect(controller.store.getState().status).toBe("succeeded");
    expect(controller.store.getState().outputs).toEqual({ greeting: "안녕, 세계" });
    expect(requests).toEqual(["POST /start"]);
    expect(onSettled).toHaveBeenCalledWith(runId);
  });

  it("입력 대기로 끝난 런에 답하면 마지막 seq 다음부터 다시 받아 같은 런을 잇는다", async () => {
    const { runId, events, resume } = wfContract.agent;
    const last = events[events.length - 1]!.seq;
    const { api, requests } = fakeApi(
      { "/start": events, [`/events/${runId}?after=${last}`]: resume },
      runId,
    );
    const controller = new WfRunController(api, { sleep: async () => undefined });
    await controller.start(() => new Request("http://test/start", { method: "POST" }));
    await settled(controller);
    expect(controller.store.getState().status).toBe("waiting_input");

    const ok = await controller.respond({ decision: "approve", session_auto_approve: true });

    expect(ok).toBe(true);
    expect(api.input).toHaveBeenCalledWith(runId, {
      decision: "approve",
      session_auto_approve: true,
    });
    await settled(controller);
    expect(requests.at(-1)).toBe(`GET /events/${runId}?after=${last}`);
    expect(controller.store.getState().status).toBe("succeeded");
    expect(controller.store.getState().pendingInteraction).toBeNull();
  });

  it("다른 경로(자식 런 화면)로 답한 뒤 resume하면 닫힌 스트림을 잇고, 열려 있으면 그대로 둔다", async () => {
    const { runId, events, resume } = wfContract.agent;
    const last = events[events.length - 1]!.seq;
    const { api, requests } = fakeApi(
      { "/start": events, [`/events/${runId}?after=${last}`]: resume },
      runId,
    );
    const controller = new WfRunController(api, { sleep: async () => undefined });
    await controller.start(() => new Request("http://test/start", { method: "POST" }));
    await settled(controller);

    controller.resume();
    await settled(controller);

    expect(requests).toEqual(["POST /start", `GET /events/${runId}?after=${last}`]);
    expect(controller.store.getState().status).toBe("succeeded");
  });

  it("시작 요청이 거절되면 그 오류를 돌려주고 알리지 않는다(화면이 칸 오류로 보인다)", async () => {
    const onError = vi.fn();
    const api: WfRunApi = {
      eventsRequest: () => new Request("http://test/events"),
      stop: vi.fn(),
      input: vi.fn(),
      fetch: async () =>
        new Response(
          JSON.stringify({
            error: { code: "invalid_inputs", message: "입력 오류", details: { q: "필수" } },
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    };
    const controller = new WfRunController(api, { onError, sleep: async () => undefined });

    const result = await controller.start(
      () => new Request("http://test/start", { method: "POST" }),
    );

    expect(result).toBeInstanceOf(ApiError);
    expect((result as ApiError).code).toBe("invalid_inputs");
    expect(onError).not.toHaveBeenCalled();
    expect(controller.store.getState().runId).toBeNull();
  });

  it("저장된 런에 붙으면 처음부터 다시 그린다", async () => {
    const { runId, events } = wfContract.iteration;
    const { api } = fakeApi({ [`/events/${runId}?after=0`]: events }, runId);
    const controller = new WfRunController(api, { sleep: async () => undefined });

    controller.attach(runId);
    await settled(controller);

    expect(controller.store.getState().iterations.iteration_1?.count).toBe(3);
    expect(controller.store.getState().status).toBe("succeeded");
  });
});
