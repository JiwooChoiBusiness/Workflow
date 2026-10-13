import { describe, expect, it, vi } from "vitest";
import type { DataRunEvent, RunEvent } from "@/api/run-events";
import { contract } from "@/test/contract";
import { fixtures } from "@/test/events";
import { backoffDelay, SseParser, streamRunEvents, type SseMessage } from "./sse";

function parseAll(chunks: string[]): SseMessage[] {
  const messages: SseMessage[] = [];
  const parser = new SseParser((message) => messages.push(message));
  chunks.forEach((chunk) => parser.feed(chunk));
  return messages;
}

// 서버가 보내는 모양(설계 6.5): id: <seq>\nevent: <type>\ndata: <JSON 한 줄>\n\n
function encode(events: RunEvent[], newline = "\n"): string {
  return events
    .map((event) => {
      const id = "seq" in event && event.seq !== undefined ? `id: ${event.seq}${newline}` : "";
      return `${id}event: ${event.type}${newline}data: ${JSON.stringify(event)}${newline}${newline}`;
    })
    .join("");
}

// 읽을 때마다 조각을 하나씩 내준다. 끊김(fail)은 조각을 모두 읽은 뒤에 일어난다.
// start()에서 바로 error()를 부르면 아직 읽지 않은 조각까지 버려진다(Streams 표준).
function streamOf(parts: (string | Uint8Array)[], options: { fail?: boolean } = {}) {
  const encoder = new TextEncoder();
  let index = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < parts.length) {
        const part = parts[index++];
        controller.enqueue(typeof part === "string" ? encoder.encode(part) : part);
      } else if (options.fail) {
        controller.error(new TypeError("network error"));
      } else {
        controller.close();
      }
    },
  });
}

function sseResponse(parts: (string | Uint8Array)[], options: { fail?: boolean } = {}) {
  return new Response(streamOf(parts, options), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("SseParser", () => {
  const text = [
    ": 주석은 무시한다",
    "id: 1",
    "event: message.delta",
    'data: {"text":"안녕"}',
    "",
    "event: ping",
    "data: {}",
    "",
    "id: 2",
    "data: 첫 줄",
    "data: 둘째 줄",
    "",
    "",
  ].join("\n");
  const expected: SseMessage[] = [
    { id: "1", event: "message.delta", data: '{"text":"안녕"}' },
    // id 필드가 없으면 마지막 id가 이어진다(HTML 표준).
    { id: "1", event: "ping", data: "{}" },
    { id: "2", event: "message", data: "첫 줄\n둘째 줄" },
  ];

  it("필드를 해석해 빈 줄마다 이벤트를 낸다", () => {
    expect(parseAll([text])).toEqual(expected);
  });

  it("청크가 어디서 잘려도 결과가 같다", () => {
    for (let cut = 1; cut < text.length; cut += 1) {
      expect(parseAll([text.slice(0, cut), text.slice(cut)])).toEqual(expected);
    }
    expect(parseAll(text.split(""))).toEqual(expected);
  });

  it("CRLF와 CR 줄바꿈도 같게 읽고, CRLF가 청크 경계에서 갈라져도 빈 줄로 보지 않는다", () => {
    const crlf = text.replaceAll("\n", "\r\n");
    expect(parseAll([crlf])).toEqual(expected);
    expect(parseAll([text.replaceAll("\n", "\r")])).toEqual(expected);
    for (let cut = 1; cut < crlf.length; cut += 1) {
      expect(parseAll([crlf.slice(0, cut), crlf.slice(cut)])).toEqual(expected);
    }
  });

  it("data가 없는 이벤트와 끝나지 않은 마지막 이벤트는 내보내지 않는다", () => {
    expect(parseAll(["event: run.started\n\n", 'id: 9\ndata: {"a":1}'])).toEqual([]);
  });

  it("콜론 뒤 공백은 하나만 뗀다", () => {
    expect(parseAll(["data:  두 칸\n\n"])).toEqual([
      { id: null, event: "message", data: " 두 칸" },
    ]);
  });
});

describe("streamRunEvents", () => {
  const events = fixtures.textOnly;

  it("바이트가 UTF-8 글자 중간에서 잘려도 이벤트를 그대로 넘긴다", async () => {
    const bytes = new TextEncoder().encode(encode(events));
    const parts: Uint8Array[] = [];
    // 7바이트씩 자르면 한글(3바이트)이 청크 경계에 걸린다.
    for (let i = 0; i < bytes.length; i += 7) parts.push(bytes.slice(i, i + 7));
    const received: RunEvent[] = [];
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/api/console/sessions/s/messages", { method: "POST" }),
      reconnect: () => {
        throw new Error("재접속하면 안 된다");
      },
      onEvent: (event) => received.push(event),
      fetch: async () => sseResponse(parts),
    });
    expect(outcome).toBe("completed");
    expect(received).toEqual(events);
  });

  it("종료 이벤트 없이 끊기면 마지막 seq 이후로 다시 붙는다", async () => {
    const cut = 10;
    const requests: string[] = [];
    const sleeps: number[] = [];
    const statuses: string[] = [];
    const received: RunEvent[] = [];
    const responses = [
      () => sseResponse([encode(events.slice(0, cut))]),
      () => sseResponse([encode(events.slice(cut))]),
    ];
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: (event) => received.push(event),
      onStatus: (status) => statuses.push(status),
      fetch: async (request) => {
        requests.push(request.url);
        return responses[requests.length - 1]();
      },
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    expect(outcome).toBe("completed");
    expect(requests).toEqual(["http://test/first", `http://test/events?after=${cut}`]);
    expect(sleeps).toEqual([1000]);
    expect(received).toEqual(events);
    expect(statuses).toEqual(["connecting", "open", "reconnecting", "open", "closed"]);
  });

  it("네트워크 오류가 이어지면 1·2·4·8초로 늘리며 다시 시도하고 받은 이벤트부터 이어 받는다", async () => {
    const sleeps: number[] = [];
    const urls: string[] = [];
    const received: RunEvent[] = [];
    let call = 0;
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: (event) => received.push(event),
      fetch: async (request) => {
        urls.push(request.url);
        call += 1;
        if (call === 1) return sseResponse([encode(events.slice(0, 3))], { fail: true });
        if (call < 5) throw new TypeError("Failed to fetch");
        return sseResponse([encode(events.slice(3))]);
      },
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    expect(outcome).toBe("completed");
    expect(sleeps).toEqual([1000, 2000, 4000, 8000]);
    expect(urls.slice(1)).toEqual(Array(4).fill("http://test/events?after=3"));
    expect(received).toEqual(events);
  });

  it("다섯 번 다시 시도해도 안 되면 실패로 끝난다", async () => {
    const sleeps: number[] = [];
    const fetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    const statuses: string[] = [];
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: () => undefined,
      onStatus: (status) => statuses.push(status),
      fetch,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });
    expect(outcome).toBe("failed");
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(sleeps).toEqual([1000, 2000, 4000, 8000, 8000]);
    expect(statuses.at(-1)).toBe("failed");
  });

  it("4xx 응답은 다시 시도하지 않는다", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: { code: "run_in_progress", message: "실행 중" } }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
    );
    let failure: unknown = null;
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: () => new Request("http://test/again"),
      onEvent: () => undefined,
      onStatus: (status, detail) => {
        if (status === "failed") failure = detail?.error;
      },
      fetch,
      sleep: async () => undefined,
    });
    expect(outcome).toBe("failed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(failure).toMatchObject({ status: 409, code: "run_in_progress" });
  });

  it("중단 신호를 받으면 다시 붙지 않고 끝난다", async () => {
    const controller = new AbortController();
    const fetch = vi.fn(async () => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    });
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: () => new Request("http://test/again"),
      onEvent: () => undefined,
      signal: controller.signal,
      fetch,
    });
    expect(outcome).toBe("aborted");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("ping은 이벤트로 넘기되 seq를 바꾸지 않는다", async () => {
    const received: RunEvent[] = [];
    const urls: string[] = [];
    let call = 0;
    await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: (event) => received.push(event),
      fetch: async (request) => {
        urls.push(request.url);
        call += 1;
        if (call === 1)
          return sseResponse([encode(events.slice(0, 2)), "event: ping\ndata: {}\n\n"]);
        return sseResponse([encode(events.slice(2))]);
      },
      sleep: async () => undefined,
    });
    expect(received[2]).toEqual({ type: "ping" });
    expect(urls[1]).toBe("http://test/events?after=2");
  });

  it("run.error만 받고 끊기면 끝난 것으로 보지 않고 다시 붙어 run.completed(failed)까지 받는다", async () => {
    const failed = fixtures.edgeCases;
    const tail = failed.at(-1) as DataRunEvent;
    expect(failed.at(-2)?.type).toBe("run.error");
    expect(tail).toMatchObject({ type: "run.completed", data: { status: "failed" } });
    const urls: string[] = [];
    const received: RunEvent[] = [];
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/first"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: (event) => received.push(event),
      fetch: async (request) => {
        urls.push(request.url);
        return urls.length === 1
          ? sseResponse([encode(failed.slice(0, -1))])
          : sseResponse([encode([tail])]);
      },
      sleep: async () => undefined,
    });
    expect(outcome).toBe("completed");
    expect(urls[1]).toBe(`http://test/events?after=${tail.seq - 1}`);
    expect(received.at(-1)).toEqual(tail);
  });

  it("재생 중 run.completed(waiting_input) 뒤에 재개 이벤트가 이어지면 마지막 이벤트로 끝을 판단한다", async () => {
    const replay = [...contract.approval.send.events, ...contract.approval.resume];
    const cut = replay.findIndex((event) => event.type === "tool_call.completed");
    const urls: string[] = [];
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/events?after=0"),
      reconnect: (after) => new Request(`http://test/events?after=${after}`),
      onEvent: () => undefined,
      fetch: async (request) => {
        urls.push(request.url);
        return urls.length === 1
          ? sseResponse([encode(replay.slice(0, cut))], { fail: true })
          : sseResponse([encode(replay.slice(cut))]);
      },
      sleep: async () => undefined,
    });
    expect(outcome).toBe("completed");
    expect(urls[1]).toBe(`http://test/events?after=${replay[cut - 1].seq}`);
  });

  it("어느 런인지 몰라 다시 붙을 수 없으면(reconnect가 null) 바로 실패로 끝난다", async () => {
    const fetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    let failure: unknown = null;
    const outcome = await streamRunEvents({
      request: () => new Request("http://test/messages", { method: "POST" }),
      reconnect: () => null,
      onEvent: () => undefined,
      onStatus: (status, detail) => {
        if (status === "failed") failure = detail?.error;
      },
      fetch,
      sleep: async () => undefined,
    });
    expect(outcome).toBe("failed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(failure).toBeInstanceOf(TypeError);
  });

  it("응답 헤더(X-Run-Id)를 첫 이벤트보다 먼저 넘긴다", async () => {
    const order: string[] = [];
    await streamRunEvents({
      request: () => new Request("http://test/messages", { method: "POST" }),
      reconnect: () => null,
      onResponse: (response) => order.push(`header:${response.headers.get("X-Run-Id")}`),
      onEvent: (event) => order.push(event.type),
      fetch: async () => {
        const response = sseResponse([encode(events.slice(0, 1)), encode(events.slice(-1))]);
        response.headers.set("X-Run-Id", "run-1");
        return response;
      },
    });
    expect(order).toEqual(["header:run-1", "run.queued", "run.completed"]);
  });

  it("백오프는 8초에서 멈춘다", () => {
    expect([1, 2, 3, 4, 5, 6].map(backoffDelay)).toEqual([1000, 2000, 4000, 8000, 8000, 8000]);
  });
});
