import { createStore, type StoreApi } from "zustand/vanilla";
import { apiFetch, isApiError } from "@/api/http";
import type { WfInteractionInput } from "@/api/run-events";
import type { ChatConnection } from "@/features/sessions/chatSession";
import { streamRunEvents } from "@/lib/sse";
import { provideWfInput, stopWfRun, wfRunEventsRequest, type WfInputBody } from "./api";
import { createWfRunStore, isWfRunOpen, type WfRunStore, type WfStreamEvent } from "./wfRunStore";

// 런 하나를 따라가는 데 쓰는 서버 호출. 콘솔(consoleWfRunApi)과 웹앱(features/webapp/flows)이 경로만 다르다.
export interface WfRunApi {
  eventsRequest: (runId: string, after: number) => Request;
  stop: (runId: string) => Promise<unknown>;
  input: (runId: string, body: WfInputBody) => Promise<unknown>;
  fetch: (request: Request) => Promise<Response>;
}

export const consoleWfRunApi: WfRunApi = {
  eventsRequest: wfRunEventsRequest,
  stop: stopWfRun,
  input: provideWfInput,
  fetch: (request) => apiFetch(request),
};

function inputBody(input: WfInteractionInput): WfInputBody {
  if ("kind" in input) return { kind: "form", values: input.values };
  if ("answer" in input) return { answer: input.answer };
  return input.decision === "approve"
    ? { decision: "approve", session_auto_approve: Boolean(input.session_auto_approve) }
    : { decision: "reject", note: input.note ?? null };
}

export interface WfRunMeta {
  connection: ChatConnection | null;
  // 런을 만드는 요청을 보내고 응답 헤더를 기다리는 중
  starting: boolean;
  stopping: boolean;
  responding: boolean;
}

export interface WfRunControllerOptions {
  onError?: (error: unknown) => void;
  // 스트림이 끝났다(완료·입력 대기·실패). 목록을 다시 읽을 때 쓴다.
  onSettled?: (runId: string | null) => void;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/*
 * 워크플로우 런 하나의 이벤트 스트림을 스토어에 잇는다. 시작(POST, 응답이 SSE)·재접속(GET events?after=)·
 * 중지·승인 응답을 맡고 화면은 store만 읽는다. 런이 입력을 기다리며 끝나면 서버가 스트림을 닫으므로,
 * 응답한 뒤에는 마지막 seq 다음부터 다시 붙는다(설계 3.7, 6.1).
 */
export class WfRunController {
  readonly store: WfRunStore;
  readonly meta: StoreApi<WfRunMeta>;
  private readonly api: WfRunApi;
  private options: WfRunControllerOptions;
  private stream: AbortController | null = null;
  private epoch = 0;

  constructor(api: WfRunApi, options: WfRunControllerOptions = {}, store?: WfRunStore) {
    this.api = api;
    this.options = options;
    this.store = store ?? createWfRunStore();
    this.meta = createStore<WfRunMeta>()(() => ({
      connection: null,
      starting: false,
      stopping: false,
      responding: false,
    }));
  }

  configure(options: WfRunControllerOptions): void {
    this.options = { ...this.options, ...options };
  }

  get runId(): string | null {
    return this.store.getState().runId;
  }

  get streaming(): boolean {
    return this.stream !== null;
  }

  /*
   * 새 런을 시작한다. 응답 헤더가 오면 true, 그 전에 실패하면 그 오류(422 입력 오류 등)를 돌려준다.
   * 오류는 부른 쪽이 화면에 보여 준다.
   */
  async start(request: () => Request): Promise<true | unknown> {
    this.epoch += 1;
    this.closeStream();
    this.store.getState().actions.reset();
    this.meta.setState({ starting: true, connection: null });
    try {
      return await this.subscribe({ request, runId: null, after: 0, quietStart: true });
    } finally {
      this.meta.setState({ starting: false });
    }
  }

  // 이미 있는 런에 붙는다. after=0이면 저장된 이벤트를 처음부터 다시 받고, 끝난 런이면 다 받은 뒤 닫힌다.
  attach(runId: string, after = 0): void {
    this.epoch += 1;
    this.closeStream();
    if (after === 0) this.store.getState().actions.reset({ runId });
    void this.subscribe({ request: () => this.api.eventsRequest(runId, after), runId, after });
  }

  // 다른 경로(자식 런 화면의 승인 등)로 런이 다시 큐에 들어갔을 수 있다. 스트림이 닫혀 있으면 이어 받는다.
  resume(): void {
    const runId = this.runId;
    if (!runId || this.stream) return;
    const after = this.store.getState().lastSeq;
    void this.subscribe({ request: () => this.api.eventsRequest(runId, after), runId, after });
  }

  async stop(): Promise<void> {
    const { runId, status } = this.store.getState();
    if (!runId || !isWfRunOpen(status) || this.meta.getState().stopping) return;
    this.meta.setState({ stopping: true });
    try {
      await this.api.stop(runId);
    } catch (error) {
      // 409면 이미 끝났다. 남은 이벤트를 받아 화면을 맞춘다.
      if (!isApiError(error, 409)) {
        this.report(error);
        return;
      }
    } finally {
      this.meta.setState({ stopping: false });
    }
    // 입력을 기다리던 런은 스트림이 닫혀 있다. 중지 결과(workflow.completed)를 받으러 다시 붙는다.
    this.resume();
  }

  // 서버가 받았으면 true. 받지 못한 이유는 알린다(onError).
  async respond(input: WfInteractionInput): Promise<boolean> {
    const result = await this.sendInput(input);
    if (result !== true && result !== null) this.report(result);
    return result === true;
  }

  /*
   * human_input 폼 응답(M19). 서버가 받았으면 true, 아니면 그 오류를 알리지 않고 돌려준다. 폼 카드가 422
   * invalid_inputs는 칸 오류로, 409(대기 시간 지남·이미 답함·중지)는 안내로 보인다.
   */
  async submitForm(values: Record<string, unknown>): Promise<true | unknown> {
    return (await this.sendInput({ kind: "form", values })) ?? false;
  }

  // 받았으면 true, 보내지 않았으면(런 없음, 이미 보내는 중) null, 아니면 그 오류.
  private async sendInput(input: WfInteractionInput): Promise<true | null | unknown> {
    const runId = this.runId;
    if (!runId || this.meta.getState().responding) return null;
    this.meta.setState({ responding: true });
    try {
      await this.api.input(runId, inputBody(input));
    } catch (error) {
      // 이미 답했거나(다른 화면) 대기 시간이 지났거나 런이 멈췄다. 서버가 아는 상태로 맞춘다.
      if (isApiError(error, 409)) this.resume();
      return error;
    } finally {
      this.meta.setState({ responding: false });
    }
    this.resume();
    return true;
  }

  reset(): void {
    this.epoch += 1;
    this.closeStream();
    this.meta.setState({ connection: null, starting: false, stopping: false, responding: false });
    this.store.getState().actions.reset();
  }

  dispose(): void {
    this.epoch += 1;
    this.closeStream();
  }

  private report(error: unknown) {
    this.options.onError?.(error);
  }

  private closeStream() {
    this.stream?.abort();
    this.stream = null;
  }

  private subscribe(plan: {
    request: () => Request;
    runId: string | null;
    after: number;
    quietStart?: boolean;
  }): Promise<true | unknown> {
    this.closeStream();
    const controller = new AbortController();
    this.stream = controller;
    const epoch = this.epoch;
    let runId = plan.runId;
    let opened = false;
    let settle: (value: true | unknown) => void = () => undefined;
    const accepted = new Promise<true | unknown>((resolve) => {
      settle = resolve;
    });
    const store = this.store;
    void streamRunEvents({
      request: plan.request,
      // 헤더도 받기 전에 끊겨 어느 런인지 모르면 다시 붙지 않는다.
      reconnect: (lastSeq) => (runId ? this.api.eventsRequest(runId, lastSeq) : null),
      onResponse: (response) => {
        runId = response.headers.get("X-Run-Id") ?? runId;
        // 첫 이벤트보다 먼저 런 id를 알려 둔다. 화면이 시작 직후 그 id로 주소·링크를 만든다.
        if (runId && store.getState().runId === null) store.getState().actions.reset({ runId });
        opened = true;
        settle(true);
      },
      onEvent: (event) => {
        if (controller.signal.aborted) return;
        store.getState().actions.applyEvent(event as unknown as WfStreamEvent);
      },
      onStatus: (status, detail) => {
        if (this.stream !== controller) return;
        if (status === "failed") {
          const error = detail?.error ?? new Error("stream_failed");
          settle(error);
          if (!opened && plan.quietStart) return;
          this.report(error);
        }
        if (status === "connecting" || status === "open" || status === "closed") {
          this.meta.setState({ connection: null });
        } else {
          this.meta.setState({ connection: { status, attempt: detail?.attempt } });
        }
      },
      signal: controller.signal,
      initialSeq: plan.after,
      fetch: this.api.fetch,
      sleep: this.options.sleep,
    }).then(
      (outcome) => {
        settle(outcome === "aborted" ? new Error("aborted") : true);
        store.getState().actions.flush();
        if (this.stream === controller) this.stream = null;
        if (outcome !== "aborted" && epoch === this.epoch) this.options.onSettled?.(runId);
      },
      (error: unknown) => {
        settle(error);
        if (this.stream === controller) this.stream = null;
      },
    );
    return accepted;
  }
}
