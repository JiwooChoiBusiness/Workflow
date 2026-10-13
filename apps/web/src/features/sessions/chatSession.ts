import { createStore, type StoreApi } from "zustand/vanilla";
import { isApiError } from "@/api/http";
import type { InteractionInput } from "@/api/run-events";
import { streamRunEvents, type StreamStatus } from "@/lib/sse";
import { createRunStore, isRunOpen, type RunStore } from "@/stores/runStore";
import { attachmentsOf, historyState } from "./history";
import type {
  MessageList,
  RunInputBody,
  RunOut,
  SessionDetail,
  SessionFileOut,
  SessionOut,
  SessionPatch,
} from "./types";

// 채팅 패널이 쓰는 서버 호출. 기본 구현은 api.ts의 consoleSessionApi이고 테스트는 가짜를 넣는다.
export interface ChatSessionApi {
  getSession: (sessionId: string) => Promise<SessionDetail>;
  listMessages: (sessionId: string) => Promise<MessageList>;
  upload: (sessionId: string, file: File) => Promise<SessionFileOut>;
  stop: (runId: string) => Promise<RunOut>;
  input: (runId: string, body: RunInputBody) => Promise<RunOut>;
  patchSession: (sessionId: string, body: SessionPatch) => Promise<SessionOut>;
  messageRequest: (sessionId: string, body: { text: string; file_ids: string[] }) => Request;
  eventsRequest: (sessionId: string, after: number, runId: string | null) => Request;
  fetch: (request: Request) => Promise<Response>;
}

export interface ChatConnection {
  status: StreamStatus;
  attempt?: number;
}

export interface ChatMeta {
  sessionId: string | null;
  // 세션 기록을 불러오는 중
  loading: boolean;
  loadError: unknown;
  connection: ChatConnection | null;
  sending: boolean;
  stopping: boolean;
}

export interface ChatSessionOptions {
  // 아직 세션이 없을 때 첫 메시지를 보내면서 만든다(미리보기는 POST /agents/{id}/preview/sessions).
  createSession?: () => Promise<string>;
  onSessionChange?: (sessionId: string | null) => void;
  // 런이 끝나 세션의 메시지·파일·작업공간이 바뀌었을 수 있다.
  onSettled?: (sessionId: string) => void;
  onError?: (error: unknown) => void;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

interface StreamPlan {
  request: () => Request;
  runId: string | null;
  after: number;
  // 첫 응답 전 실패는 부른 쪽(메시지 보내기)이 알린다.
  quietStart?: boolean;
}

/*
 * 세션 하나의 채팅 상태를 서버와 맞춘다(설계 7.4). 세션을 열면 기록을 불러오고 진행 중인 런이 있으면
 * events?after=0으로 다시 구독한다. 보내기·승인·질문 답·중지는 REST로 하고 이벤트는 SSE 하나로만 받는다.
 */
export class ChatSessionController {
  readonly store: RunStore;
  readonly meta: StoreApi<ChatMeta>;
  private readonly api: ChatSessionApi;
  private options: ChatSessionOptions;
  private stream: AbortController | null = null;
  // 세션을 바꾸거나 초기화하면 늘린다. 그 전에 시작한 비동기 작업의 결과는 버린다.
  private epoch = 0;

  constructor(api: ChatSessionApi, options: ChatSessionOptions = {}, store?: RunStore) {
    this.api = api;
    this.options = options;
    this.store = store ?? createRunStore();
    this.meta = createStore<ChatMeta>()(() => ({
      sessionId: null,
      loading: false,
      loadError: null,
      connection: null,
      sending: false,
      stopping: false,
    }));
  }

  // 화면이 렌더링마다 최신 콜백을 넘긴다.
  configure(options: ChatSessionOptions): void {
    this.options = { ...this.options, ...options };
  }

  get sessionId(): string | null {
    return this.meta.getState().sessionId;
  }

  private setMeta(patch: Partial<ChatMeta>) {
    this.meta.setState(patch);
  }

  private setSession(sessionId: string | null) {
    if (this.sessionId === sessionId) return;
    this.setMeta({ sessionId });
    this.options.onSessionChange?.(sessionId);
  }

  private report(error: unknown) {
    this.options.onError?.(error);
  }

  async open(sessionId: string | null): Promise<void> {
    const epoch = ++this.epoch;
    this.closeStream();
    this.setMeta({ sessionId, loading: Boolean(sessionId), loadError: null, connection: null });
    this.store.getState().actions.reset({ sessionId });
    if (!sessionId) return;
    try {
      // 상세를 먼저 읽는다. 그 뒤에 읽은 메시지에는 상세가 본 런들의 행이 모두 들어 있다.
      const detail = await this.api.getSession(sessionId);
      const messages = await this.api.listMessages(sessionId);
      if (epoch !== this.epoch) return;
      this.store
        .getState()
        .actions.reset(historyState(messages.items, detail, { hasMore: messages.has_more }));
      const active = detail.active_run;
      if (active) {
        this.subscribe(sessionId, {
          request: () => this.api.eventsRequest(sessionId, 0, active.id),
          runId: active.id,
          after: 0,
        });
      }
    } catch (error) {
      if (epoch !== this.epoch) return;
      if (isApiError(error, 404)) {
        // 지워진 세션이다. 다음 메시지를 보낼 때 새로 만든다.
        this.setSession(null);
        this.store.getState().actions.reset();
        return;
      }
      this.setMeta({ loadError: error });
    } finally {
      if (epoch === this.epoch) this.setMeta({ loading: false });
    }
  }

  // 서버 상태를 다시 읽어 맞춘다(409, 끊긴 전송 뒤).
  resync(): Promise<void> {
    return this.open(this.sessionId);
  }

  private async ensureSession(): Promise<string> {
    const current = this.sessionId;
    if (current) return current;
    if (!this.options.createSession) throw new Error("세션이 없다");
    const created = await this.options.createSession();
    this.setSession(created);
    this.store.getState().actions.reset({ sessionId: created });
    return created;
  }

  // 받아들여졌으면 true. false면 화면이 입력한 글을 되돌려 놓는다.
  async send(text: string, files: readonly File[] = []): Promise<boolean> {
    if (this.meta.getState().sending) return false;
    const epoch = this.epoch;
    this.setMeta({ sending: true });
    try {
      return await this.sendOnce(text, files, epoch, true);
    } catch (error) {
      this.report(error);
      return false;
    } finally {
      this.setMeta({ sending: false });
    }
  }

  private async sendOnce(
    text: string,
    files: readonly File[],
    epoch: number,
    retryMissing: boolean,
  ): Promise<boolean> {
    const sessionId = await this.ensureSession();
    const uploaded: SessionFileOut[] = [];
    try {
      for (const file of files) uploaded.push(await this.api.upload(sessionId, file));
    } catch (error) {
      if (isApiError(error, 404) && retryMissing) return this.sendInNewSession(text, files, epoch);
      throw error;
    }
    if (epoch !== this.epoch) return false;
    const actions = this.store.getState().actions;
    const itemId = actions.addUserMessage(text, {
      files: attachmentsOf({
        files: uploaded.map((file) => ({
          session_file_id: file.id,
          path: file.path,
          size: file.size_bytes,
          downloadable: file.downloadable,
          filename: file.filename,
        })),
      }),
    });
    const accepted = await this.subscribe(sessionId, {
      request: () =>
        this.api.messageRequest(sessionId, { text, file_ids: uploaded.map((file) => file.id) }),
      runId: null,
      after: 0,
      quietStart: true,
    });
    if (accepted === true) return true;
    actions.removeItem(itemId);
    if (epoch !== this.epoch) return false;
    if (isApiError(accepted, 404) && retryMissing) return this.sendInNewSession(text, files, epoch);
    this.report(accepted);
    if (isApiError(accepted, 409) || !isApiError(accepted) || accepted.status >= 500) {
      // 다른 곳에서 시작한 런이 있거나 요청이 서버에 닿았는지 모른다. 서버 상태로 맞춘다.
      void this.resync();
    }
    return false;
  }

  // 세션이 지워졌다(로그 화면에서 삭제). 새 세션을 만들어 한 번만 다시 보낸다.
  private sendInNewSession(text: string, files: readonly File[], epoch: number) {
    this.setSession(null);
    this.store.getState().actions.reset();
    return this.sendOnce(text, files, epoch, false);
  }

  // 서버가 받았으면 true. 질문 답을 입력창에서 보냈다면 false일 때 글을 되돌려 놓는다.
  async respond(runId: string, input: InteractionInput): Promise<boolean> {
    const sessionId = this.sessionId;
    if (!sessionId) return false;
    const body: RunInputBody =
      "answer" in input
        ? { answer: input.answer }
        : input.decision === "approve"
          ? { decision: "approve", session_auto_approve: Boolean(input.session_auto_approve) }
          : { decision: "reject", note: input.note ?? null };
    try {
      await this.api.input(runId, body);
    } catch (error) {
      this.report(error);
      // 이미 답했거나(다른 탭) 대기 시간이 지났다. 서버가 아는 상태를 다시 보여 준다.
      if (isApiError(error, 409)) void this.resync();
      return false;
    }
    // 202 뒤에는 남은 이벤트를 다시 받는다. 첫 이벤트가 interaction.resolved다(12.2).
    const after = this.store.getState().lastSeq[runId] ?? 0;
    this.subscribe(sessionId, {
      request: () => this.api.eventsRequest(sessionId, after, runId),
      runId,
      after,
    });
    return true;
  }

  async stop(): Promise<void> {
    const sessionId = this.sessionId;
    const { runId, status } = this.store.getState();
    if (!sessionId || !runId || !isRunOpen(status) || this.meta.getState().stopping) return;
    this.setMeta({ stopping: true });
    try {
      await this.api.stop(runId);
    } catch (error) {
      if (isApiError(error, 409)) void this.resync();
      else this.report(error);
      return;
    } finally {
      this.setMeta({ stopping: false });
    }
    // 입력을 기다리던 런은 서버가 스트림을 닫았다(닫는 중일 수도 있다). 중지 결과(run.completed)를 받으러
    // 다시 붙는다. 도는 중이던 런의 결과는 열려 있는 스트림으로 온다.
    const now = this.store.getState().status;
    if (isRunOpen(now) && (!this.stream || now === "waiting_input")) {
      const after = this.store.getState().lastSeq[runId] ?? 0;
      this.subscribe(sessionId, {
        request: () => this.api.eventsRequest(sessionId, after, runId),
        runId,
        after,
      });
    }
  }

  async setAutoApprove(on: boolean): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) return;
    try {
      const session = await this.api.patchSession(sessionId, {
        approval_override: on ? "off" : null,
      });
      this.store.getState().actions.setSessionAutoApprove(session.approval_override === "off");
    } catch (error) {
      this.report(error);
    }
  }

  // 새 대화를 시작한다. 진행 중인 런은 멈추고, 세션은 다음 메시지를 보낼 때 새로 만든다.
  async reset(): Promise<void> {
    const { runId, status } = this.store.getState();
    if (this.sessionId && runId && isRunOpen(status)) {
      try {
        await this.api.stop(runId);
      } catch {
        // 이미 끝났으면 그대로 둔다.
      }
    }
    this.epoch += 1;
    this.closeStream();
    this.setSession(null);
    this.setMeta({ loading: false, loadError: null, connection: null });
    this.store.getState().actions.reset();
  }

  dispose(): void {
    this.epoch += 1;
    this.closeStream();
  }

  private closeStream() {
    this.stream?.abort();
    this.stream = null;
  }

  /*
   * 스트림 하나를 연다(이전 것은 닫는다). 응답 헤더가 오면 true, 그 전에 실패하면 그 오류로 풀린다.
   * 스트림 자체는 run.completed를 받을 때까지 뒤에서 계속 돈다.
   */
  private subscribe(sessionId: string, plan: StreamPlan): Promise<true | unknown> {
    this.closeStream();
    const controller = new AbortController();
    this.stream = controller;
    let runId = plan.runId;
    let settle: (value: true | unknown) => void = () => undefined;
    const accepted = new Promise<true | unknown>((resolve) => {
      settle = resolve;
    });
    const store = this.store;
    let opened = false;
    const done = (outcome: string) => {
      if (this.stream === controller) {
        this.stream = null;
        if (outcome === "completed") this.setMeta({ connection: null });
      }
      if (outcome !== "aborted") this.options.onSettled?.(sessionId);
    };
    void streamRunEvents({
      request: plan.request,
      // 어느 런인지 모르는 채로 끊겼으면(헤더도 받기 전) 다시 붙지 않고 세션을 다시 읽는다.
      reconnect: (lastSeq) => (runId ? this.api.eventsRequest(sessionId, lastSeq, runId) : null),
      onResponse: (response) => {
        runId = response.headers.get("X-Run-Id") ?? runId;
        opened = true;
        settle(true);
      },
      onEvent: (event) => {
        // 닫은 스트림이 이미 받아 둔 조각은 버린다(초기화·세션 전환 뒤에 섞이지 않게).
        if (controller.signal.aborted) return;
        if (event.type !== "ping") runId ??= event.run_id;
        store.getState().actions.applyEvent(event);
      },
      onStatus: (status, detail) => {
        if (this.stream !== controller) return;
        if (status === "failed") {
          const error = detail?.error ?? new Error("stream_failed");
          settle(error);
          if (!opened && plan.quietStart) return;
          if (!opened) this.report(error);
        }
        if (status === "connecting" || status === "open" || status === "closed") {
          this.setMeta({ connection: null });
        } else {
          this.setMeta({ connection: { status, attempt: detail?.attempt } });
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
        done(outcome);
      },
      (error: unknown) => {
        settle(error);
        done("failed");
      },
    );
    return accepted;
  }
}
