import { apiFetch, ApiError, toApiError } from "@/api/http";
import { isTerminalEventType, type RunEvent } from "@/api/run-events";

export interface SseMessage {
  // 마지막으로 받은 id 필드 값. SSE 규약상 이후 이벤트에도 그대로 이어진다.
  id: string | null;
  event: string;
  data: string;
}

// text/event-stream 줄 파서(HTML 표준의 해석 규칙). 청크가 어디서 잘려 들어와도 결과가 같아야 한다.
export class SseParser {
  private buffer = "";
  private skipLeadingLf = false;
  private dataLines: string[] = [];
  private eventType = "";
  private lastEventId: string | null = null;
  private readonly onMessage: (message: SseMessage) => void;

  constructor(onMessage: (message: SseMessage) => void) {
    this.onMessage = onMessage;
  }

  feed(chunk: string): void {
    let text = chunk;
    // 앞 청크가 \r로 끝났다면 이 청크의 \n은 같은 줄바꿈(\r\n)의 뒷부분이다.
    if (this.skipLeadingLf && text.length > 0) {
      if (text.startsWith("\n")) text = text.slice(1);
      this.skipLeadingLf = false;
    }
    const buffer = this.buffer + text;
    let lineStart = 0;
    for (let i = 0; i < buffer.length; i++) {
      const ch = buffer.charCodeAt(i);
      if (ch !== 10 && ch !== 13) continue;
      this.processLine(buffer.slice(lineStart, i));
      if (ch === 13) {
        if (i + 1 < buffer.length) {
          if (buffer.charCodeAt(i + 1) === 10) i++;
        } else {
          this.skipLeadingLf = true;
        }
      }
      lineStart = i + 1;
    }
    this.buffer = buffer.slice(lineStart);
  }

  private processLine(line: string): void {
    if (line === "") {
      this.dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "data":
        this.dataLines.push(value);
        break;
      case "event":
        this.eventType = value;
        break;
      case "id":
        if (!value.includes("\0")) this.lastEventId = value;
        break;
      default:
        // retry 등은 쓰지 않는다. 재접속 간격은 클라이언트가 정한다(설계 7.4).
        break;
    }
  }

  private dispatch(): void {
    if (this.dataLines.length === 0) {
      this.eventType = "";
      return;
    }
    const message: SseMessage = {
      id: this.lastEventId,
      event: this.eventType || "message",
      data: this.dataLines.join("\n"),
    };
    this.dataLines = [];
    this.eventType = "";
    this.onMessage(message);
  }
}

export type StreamStatus = "connecting" | "open" | "reconnecting" | "closed" | "failed";
export type StreamOutcome = "completed" | "aborted" | "failed";

export interface StreamStatusDetail {
  attempt?: number;
  delayMs?: number;
  error?: unknown;
}

export interface RunStreamOptions {
  // 첫 연결. 메시지 전송(POST, 응답이 SSE)이나 이벤트 재구독(GET ...events?after=)
  request: () => Request;
  // 연결이 끊겼을 때 마지막으로 받은 seq 이후를 다시 받는 요청. 어느 런인지 모르면 null(다시 붙지 않는다).
  reconnect: (lastSeq: number) => Request | null;
  onEvent: (event: RunEvent) => void;
  onStatus?: (status: StreamStatus, detail?: StreamStatusDetail) => void;
  // 연결마다 응답 헤더(X-Run-Id 등)를 첫 이벤트보다 먼저 넘긴다.
  onResponse?: (response: Response) => void;
  signal?: AbortSignal;
  initialSeq?: number;
  maxRetries?: number;
  fetch?: (request: Request) => Promise<Response>;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

export const RECONNECT_MAX_RETRIES = 5;
const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 8000;

// 1·2·4·8초, 이후는 8초(설계 7.4)
export function backoffDelay(attempt: number): number {
  return Math.min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_MAX_MS);
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function toRunEvent(message: SseMessage): RunEvent | null {
  if (message.event === "ping") return { type: "ping" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(message.data);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const record = parsed as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : message.event;
  let seq = typeof record.seq === "number" ? record.seq : Number(message.id);
  if (!Number.isFinite(seq)) seq = 0;
  return { ...record, type, seq } as RunEvent;
}

// 서버는 완료 이벤트(run.completed·workflow.completed, 입력 대기 포함) 뒤에 연결을 닫는다. run.error 뒤에는
// run.completed(failed)가 온다. 재생 중에는 완료 이벤트(waiting_input) 뒤에 재개된 이벤트가 이어지므로 마지막
// 이벤트로 판단한다.
function isTerminal(event: RunEvent): boolean {
  return isTerminalEventType(event.type);
}

function isRetryableStatus(status: number): boolean {
  return status >= 500 || status === 408 || status === 429;
}

export async function streamRunEvents(options: RunStreamOptions): Promise<StreamOutcome> {
  const fetchImpl = options.fetch ?? ((request: Request) => apiFetch(request));
  const sleep = options.sleep ?? defaultSleep;
  const maxRetries = options.maxRetries ?? RECONNECT_MAX_RETRIES;
  const { signal, onStatus } = options;

  let lastSeq = options.initialSeq ?? 0;
  let attempt = 0;
  let terminal = false;
  let received: boolean;
  let request = options.request();

  const parser = new SseParser((message) => {
    const event = toRunEvent(message);
    if (!event) return;
    if (event.type !== "ping") {
      if (typeof event.seq === "number" && event.seq > lastSeq) lastSeq = event.seq;
      terminal = isTerminal(event);
    }
    received = true;
    options.onEvent(event);
  });

  onStatus?.("connecting");
  for (;;) {
    if (signal?.aborted) return "aborted";
    let failure: unknown = null;
    received = false;
    try {
      const response = await fetchImpl(request);
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const error = toApiError(response, body);
        if (!isRetryableStatus(response.status)) {
          onStatus?.("failed", { error });
          return "failed";
        }
        throw error;
      }
      if (!response.body) throw new ApiError(0, "empty_stream", "응답 본문이 없다");
      options.onResponse?.(response);
      onStatus?.("open");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          parser.feed(decoder.decode(value, { stream: true }));
        }
        parser.feed(decoder.decode());
      } finally {
        reader.releaseLock();
      }
      if (terminal) {
        onStatus?.("closed");
        return "completed";
      }
    } catch (error) {
      if (signal?.aborted) return "aborted";
      if (terminal) {
        onStatus?.("closed");
        return "completed";
      }
      failure = error;
    }

    // 종료 이벤트 없이 끊겼다. 이번 연결에서 이벤트를 받았으면 연결은 살아 있었던 것이라 횟수를 처음부터 센다.
    if (received) attempt = 0;
    attempt += 1;
    const next = attempt > maxRetries ? null : options.reconnect(lastSeq);
    if (!next) {
      onStatus?.("failed", { attempt: attempt - 1, error: failure });
      return "failed";
    }
    const delayMs = backoffDelay(attempt);
    onStatus?.("reconnecting", { attempt, delayMs, error: failure });
    try {
      await sleep(delayMs, signal);
    } catch {
      return "aborted";
    }
    request = next;
  }
}
