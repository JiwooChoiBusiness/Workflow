import { createStore, type StoreApi } from "zustand/vanilla";
import { isApiError } from "@/api/http";
import type { WfInteractionInput } from "@/api/run-events";
import type { WfConversationDetail, WfFileOut, WfMessageCreate, WfMessageList } from "@/api/types";
import { createRunStore, type PendingInteraction, type RunStore } from "@/stores/runStore";
import {
  attachmentsOf,
  chatView,
  EMPTY_HISTORY,
  EMPTY_VIEW,
  historyOf,
  reuseItems,
  type ChatHistory,
  type ChatTurn,
  type ChatView,
} from "./chatAdapter";
import type { WfRunController } from "./wfRunController";
import type { WfRunStatus, WfRunStoreState } from "./wfRunStore";

// 대화 하나를 다루는 서버 호출. 콘솔(consoleChatApi)과 웹앱(webChatApi)이 경로만 다르다.
export interface ChatflowApi {
  getConversation: (conversationId: string) => Promise<WfConversationDetail>;
  listMessages: (conversationId: string) => Promise<WfMessageList>;
  // 응답이 SSE이고 X-Run-Id 헤더가 붙는다(설계 6.1).
  messageRequest: (conversationId: string, body: WfMessageCreate) => Request;
  upload: (file: File) => Promise<WfFileOut>;
  stop: (runId: string) => Promise<unknown>;
  // 메시지에 붙은 업로드의 내려받기 주소
  fileUrl: (conversationId: string, fileId: string) => string;
}

export interface ChatflowMeta {
  conversationId: string | null;
  detail: WfConversationDetail | null;
  history: ChatHistory;
  turn: ChatTurn | null;
  // 대화 기록을 불러오는 중이거나 불러오지 못했다.
  loading: boolean;
  loadError: unknown;
  sending: boolean;
}

export interface ChatflowSessionOptions {
  // 대화가 없을 때 첫 메시지를 보내면서 만든다.
  createConversation?: () => Promise<string>;
  onConversationChange?: (conversationId: string | null) => void;
  // 서버가 메시지를 받았다. 첫 메시지면 대화 제목이 생기므로 대화 목록을 다시 읽는다.
  onSent?: (conversationId: string) => void;
  // 턴이 끝났거나 입력을 기다리며 멈췄다. 대화 목록·대화 변수를 다시 읽을 때 쓴다.
  onSettled?: (conversationId: string) => void;
  onError?: (error: unknown) => void;
}

const SETTLED: ReadonlySet<WfRunStatus> = new Set([
  "waiting_input",
  "succeeded",
  "failed",
  "cancelled",
]);

function samePending(a: PendingInteraction | null, b: PendingInteraction | null): boolean {
  return a === b || (a !== null && b !== null && a.interaction_id === b.interaction_id);
}

/*
 * 챗플로우 대화 하나를 서버와 맞춘다(설계 7.3 ChatflowPreview, 7.4 웹앱 채팅). 대화 기록은 REST로 읽고, 턴의
 * 런은 워크플로우 런 컨트롤러(wfRunController)로 따라간다. 편집기는 실행 패널·캔버스와 같은 컨트롤러를
 * 넘기므로 진행 중인 턴의 노드가 노드 타임라인과 캔버스 상태 점에 그대로 보인다. 화면에 그릴 목록은
 * chatAdapter가 둘을 합쳐 만들고 1단계 채팅 목록이 읽는 런 스토어(view)에도 옮겨 둔다.
 */
export class ChatflowSession {
  readonly meta: StoreApi<ChatflowMeta>;
  readonly chat: StoreApi<ChatView>;
  // MessageList(1단계)가 RunStoreProvider로 읽는 목록·승인 카드
  readonly view: RunStore;
  readonly runs: WfRunController;
  private readonly api: ChatflowApi;
  private options: ChatflowSessionOptions;
  private opening: string | null = null;
  // 대화를 바꾸거나 새로 시작하면 늘린다. 그 전에 시작한 비동기 작업의 결과는 버린다.
  private epoch = 0;
  private refreshing: Promise<void> | null = null;
  private refreshAgain = false;

  constructor(api: ChatflowApi, runs: WfRunController, options: ChatflowSessionOptions = {}) {
    this.api = api;
    this.runs = runs;
    this.options = options;
    this.meta = createStore<ChatflowMeta>()(() => ({
      conversationId: null,
      detail: null,
      history: EMPTY_HISTORY,
      turn: null,
      loading: false,
      loadError: null,
      sending: false,
    }));
    this.chat = createStore<ChatView>()(() => EMPTY_VIEW);
    this.view = createRunStore();
    // 구독은 세션이 사는 동안 둔다. 세션과 런 컨트롤러는 같은 화면이 함께 만들고 함께 버린다.
    this.meta.subscribe((state, previous) => {
      if (state.history !== previous.history || state.turn !== previous.turn) this.recompute();
    });
    runs.store.subscribe((state, previous) => this.onRunChange(state, previous));
    this.recompute();
  }

  // 1단계 채팅 목록(MessageList)의 fileHref. 대화가 정해지기 전에는 링크가 없다.
  readonly fileHref = (fileId: string): string | null => {
    const conversationId = this.conversationId;
    return conversationId === null ? null : this.api.fileUrl(conversationId, fileId);
  };

  configure(options: ChatflowSessionOptions): void {
    this.options = { ...this.options, ...options };
  }

  get conversationId(): string | null {
    return this.meta.getState().conversationId;
  }

  // 대화 첫머리에 보일 시작 문구(settings.webapp.opening_statement).
  setOpening(text: string | null): void {
    if (text === this.opening) return;
    this.opening = text;
    this.recompute();
  }

  // 대화를 연다. 진행 중인 런(입력 대기 포함)이 있으면 그 런의 이벤트를 처음부터 다시 받는다.
  async open(conversationId: string | null): Promise<void> {
    const epoch = ++this.epoch;
    this.meta.setState({
      conversationId,
      detail: null,
      history: EMPTY_HISTORY,
      turn: null,
      loading: conversationId !== null,
      loadError: null,
    });
    if (conversationId === null) return;
    try {
      // 상세를 먼저 읽는다. 그 뒤에 읽은 메시지에는 상세가 본 런들의 행이 모두 들어 있다.
      const detail = await this.api.getConversation(conversationId);
      const messages = await this.api.listMessages(conversationId);
      if (epoch !== this.epoch) return;
      this.meta.setState({ detail, history: historyOf(messages, detail), loading: false });
      const active = detail.active_run;
      if (active && (this.runs.runId !== active.id || !this.runs.streaming)) {
        this.runs.attach(active.id, 0);
      }
    } catch (error) {
      if (epoch !== this.epoch) return;
      if (isApiError(error, 404)) {
        // 지워진 대화다. 다음 메시지를 보낼 때 새로 만든다.
        this.meta.setState({ loading: false });
        this.setConversation(null);
        return;
      }
      this.meta.setState({ loading: false, loadError: error });
    }
  }

  resync(): Promise<void> {
    return this.open(this.conversationId);
  }

  /*
   * 메시지를 보낸다. 받아들여졌으면 true, 알릴 것 없이 그만뒀으면(보내는 중, 그사이 대화를 바꿈) false, 아니면
   * 그 오류를 돌려준다(422 입력 오류·검증 오류는 화면이 칸과 안내로 보인다). 이미 진행 중인 런이 있으면
   * (409 run_in_progress) 대화를 다시 읽어 그 런에 붙는다.
   */
  async send(
    text: string,
    files: readonly File[],
    inputs: Record<string, unknown>,
  ): Promise<boolean | unknown> {
    if (this.meta.getState().sending) return false;
    const epoch = this.epoch;
    this.meta.setState({ sending: true });
    try {
      return await this.sendOnce(text, files, inputs, epoch, true);
    } catch (error) {
      return error;
    } finally {
      this.meta.setState({ sending: false });
    }
  }

  private async sendOnce(
    text: string,
    files: readonly File[],
    inputs: Record<string, unknown>,
    epoch: number,
    retryMissing: boolean,
  ): Promise<boolean | unknown> {
    const conversationId = await this.ensureConversation();
    const uploaded: WfFileOut[] = [];
    for (const file of files) uploaded.push(await this.api.upload(file));
    if (epoch !== this.epoch) return false;
    this.meta.setState({ turn: { runId: null, text, files: attachmentsOf(uploaded, false) } });
    const accepted = await this.runs.start(() =>
      this.api.messageRequest(conversationId, {
        text,
        inputs,
        files: uploaded.map((file) => file.id),
        response_mode: "streaming",
      }),
    );
    if (accepted === true) {
      if (epoch === this.epoch) {
        this.meta.setState((state) => ({
          turn: state.turn ? { ...state.turn, runId: this.runs.runId } : null,
        }));
      }
      this.options.onSent?.(conversationId);
      return true;
    }
    if (epoch !== this.epoch) return accepted;
    this.meta.setState({ turn: null });
    if (isApiError(accepted, 404) && retryMissing) {
      // 대화가 다른 곳(로그 화면)에서 지워졌다. 새 대화를 만들어 한 번만 다시 보낸다.
      this.meta.setState({ history: EMPTY_HISTORY, detail: null });
      this.setConversation(null);
      return this.sendOnce(text, files, inputs, epoch, false);
    }
    if (isApiError(accepted, 409) || !isApiError(accepted) || accepted.status >= 500) {
      // 다른 곳에서 시작한 런이 있거나 요청이 서버에 닿았는지 모른다. 서버 상태로 맞추고 그 런에 붙는다.
      void this.open(conversationId);
    }
    return accepted;
  }

  // 서버가 받았으면 true.
  respond(input: WfInteractionInput): Promise<boolean> {
    return this.runs.respond(input);
  }

  // human_input 폼 응답. 서버가 받았으면 true, 아니면 그 오류(폼 카드가 보인다).
  submitForm(values: Record<string, unknown>): Promise<true | unknown> {
    return this.runs.submitForm(values);
  }

  async stop(): Promise<void> {
    const runId = this.chat.getState().openRunId;
    if (!runId) return;
    if (this.runs.runId === runId) {
      await this.runs.stop();
      return;
    }
    // 이 대화의 런을 지금 따라가고 있지 않다(연결이 끊긴 채 남은 런). 멈추고 대화를 다시 읽는다.
    try {
      await this.api.stop(runId);
    } catch (error) {
      if (!isApiError(error, 409)) this.report(error);
    }
    await this.open(this.conversationId);
  }

  // 새 대화를 시작한다. 대화는 다음 메시지를 보낼 때 만든다. 돌던 런은 그 대화에서 계속 돈다.
  startNew(): void {
    this.epoch += 1;
    this.meta.setState({
      detail: null,
      history: EMPTY_HISTORY,
      turn: null,
      loading: false,
      loadError: null,
    });
    this.setConversation(null);
  }

  // 화면이 내려갈 때. 진행 중인 비동기 작업의 결과를 버린다(StrictMode가 효과를 다시 돌려도 다시 열 수 있다).
  dispose(): void {
    this.epoch += 1;
  }

  private report(error: unknown) {
    this.options.onError?.(error);
  }

  private setConversation(conversationId: string | null) {
    if (this.conversationId === conversationId) return;
    this.meta.setState({ conversationId });
    this.options.onConversationChange?.(conversationId);
  }

  private async ensureConversation(): Promise<string> {
    const current = this.conversationId;
    if (current) return current;
    if (!this.options.createConversation) throw new Error("대화가 없다");
    const created = await this.options.createConversation();
    this.setConversation(created);
    return created;
  }

  // 이 대화의 런인지. 보낸 직후(런 id를 받기 전)에 오는 이벤트도 이 대화 것이다.
  private owns(runId: string): boolean {
    const { turn, history, sending } = this.meta.getState();
    if (turn?.runId === runId || (sending && turn !== null && turn.runId === null)) return true;
    return history.messages.some((message) => message.run_id === runId);
  }

  private onRunChange(state: WfRunStoreState, previous: WfRunStoreState) {
    if (
      state.runId !== previous.runId ||
      state.status !== previous.status ||
      state.answerText !== previous.answerText ||
      state.answerResumedAt !== previous.answerResumedAt ||
      state.pendingInteraction !== previous.pendingInteraction ||
      state.error !== previous.error
    ) {
      this.recompute();
    }
    if (
      state.runId !== null &&
      state.runId === previous.runId &&
      state.status !== previous.status &&
      SETTLED.has(state.status) &&
      this.owns(state.runId)
    ) {
      void this.refresh();
    }
  }

  // 턴이 멈추거나 끝나면 메시지(서버가 쓴 답 글·상태)와 대화 상세를 다시 읽는다. 겹치면 한 번 더 읽는다.
  private refresh(): Promise<void> {
    if (this.refreshing) {
      this.refreshAgain = true;
      return this.refreshing;
    }
    this.refreshing = (async () => {
      do {
        this.refreshAgain = false;
        await this.reload();
      } while (this.refreshAgain);
    })().finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }

  private async reload(): Promise<void> {
    const conversationId = this.conversationId;
    if (!conversationId) return;
    const epoch = this.epoch;
    try {
      const detail = await this.api.getConversation(conversationId);
      const messages = await this.api.listMessages(conversationId);
      if (epoch !== this.epoch) return;
      this.meta.setState({ detail, history: historyOf(messages, detail) });
      this.options.onSettled?.(conversationId);
    } catch (error) {
      if (epoch === this.epoch && !isApiError(error, 404)) this.report(error);
    }
  }

  private recompute() {
    const { history, turn } = this.meta.getState();
    const next = chatView(history, turn, this.runs.store.getState(), { opening: this.opening });
    const current = this.chat.getState();
    const items = reuseItems(current.items, next.items);
    const pending = samePending(current.pending, next.pending) ? current.pending : next.pending;
    if (
      items !== current.items ||
      pending !== current.pending ||
      next.form !== current.form ||
      next.pendingNodeId !== current.pendingNodeId ||
      next.openRunId !== current.openRunId ||
      next.status !== current.status ||
      next.liveRunId !== current.liveRunId ||
      next.started !== current.started
    ) {
      this.chat.setState({ ...next, items, pending }, true);
      this.view.setState({ items, pendingInteraction: pending, status: next.status });
    }
  }
}
