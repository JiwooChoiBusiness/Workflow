// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DataRunEvent, RunEventOf } from "@/api/run-events";
import { historyState } from "@/features/sessions/history";
import type { ChatSessionApi } from "@/features/sessions/chatSession";
import type { MessageList, SessionDetail } from "@/features/sessions/types";
import { createRunStore, type RunStore } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { contract, sseBody } from "@/test/contract";
import { ChildRunCard } from "./ChildRunCard";
import { ChildScopeContext, type ChildScope } from "./childScope";

afterEach(cleanup);

const o = contract.orchestration;
const parentSessionId = o.parentDetailWaiting.id;
const parentRunId = o.send[0].run_id;
const started = o.send.filter(
  (event): event is RunEventOf<"child_run.started"> => event.type === "child_run.started",
);
const shell = started[0].data;
const extract = started[1].data;
const firstChild = o.send.findIndex((event) => event.type === "child_run.started");
const waitingSeq = o.childShell.eventsWaiting[o.childShell.eventsWaiting.length - 1].seq;

function sseResponse(events: readonly DataRunEvent[], keepOpen: boolean): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(sseBody(events)));
      if (!keepOpen) controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

interface Fake {
  details: Record<string, SessionDetail>;
  messages?: Record<string, MessageList>;
  // "세션:after" → 그 뒤의 이벤트. open이면 스트림을 닫지 않는다(진행 중인 런).
  streams: Record<string, { events: DataRunEvent[]; open?: boolean }>;
}

function fakeApi(fake: Fake) {
  const api = {
    getSession: vi.fn(async (sessionId: string) => {
      const detail = fake.details[sessionId];
      if (!detail) throw new Error(`없는 세션 ${sessionId}`);
      return detail;
    }),
    listMessages: vi.fn(
      async (sessionId: string) => fake.messages?.[sessionId] ?? { items: [], has_more: false },
    ),
    upload: vi.fn(),
    stop: vi.fn(async () => o.childShell.input),
    input: vi.fn(async () => o.childShell.input),
    patchSession: vi.fn(),
    messageRequest: vi.fn(),
    eventsRequest: (sessionId: string, after: number, runId: string | null) =>
      new Request(`http://test/sessions/${sessionId}/events?after=${after}&run_id=${runId ?? ""}`),
    fetch: vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const sessionId = url.pathname.split("/")[2];
      const stream = fake.streams[`${sessionId}:${url.searchParams.get("after")}`];
      if (!stream) return new Response(null, { status: 404 });
      return sseResponse(stream.events, stream.open ?? false);
    }),
  } satisfies ChatSessionApi;
  return api;
}

function renderCards(store: RunStore, api: ChatSessionApi, toolCallIds: string[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const scope: ChildScope = {
    api,
    fileUrl: (sessionId, fileId) => `/files/${sessionId}/${fileId}`,
    markAutoApprove: () => store.getState().actions.setSessionAutoApprove(true),
  };
  render(
    <QueryClientProvider client={client}>
      <RunStoreProvider store={store}>
        <ChildScopeContext value={scope}>
          {toolCallIds.map((id) => (
            <ChildRunCard key={id} toolCallId={id} />
          ))}
        </ChildScopeContext>
      </RunStoreProvider>
    </QueryClientProvider>,
  );
  return { user: userEvent.setup() };
}

function parentStore(events: readonly DataRunEvent[]): RunStore {
  const store = createRunStore();
  store.getState().actions.applyEvents(events);
  return store;
}

describe("ChildRunCard", () => {
  it("부모가 기다리는 동안 자식 세션을 따라가 카드 안에서 승인하고, 자동 승인을 부모 대화에도 켠다", async () => {
    const store = parentStore(o.send.slice(0, firstChild + 1));
    const api = fakeApi({
      details: { [shell.child_session_id]: o.childShell.detailWaiting },
      messages: { [shell.child_session_id]: o.childShell.messagesWaiting },
      streams: {
        [`${shell.child_session_id}:0`]: { events: o.childShell.eventsWaiting },
        [`${shell.child_session_id}:${waitingSeq}`]: { events: o.childShell.resume },
      },
    });
    const { user } = renderCards(store, api, [shell.tool_call_id]);

    const card = screen.getByRole("region", { name: "하위 에이전트 셸 담당" });
    expect(within(card).getByText("shellbot")).toBeTruthy();
    // 접힌 카드는 과업의 앞부분을 보여 준다.
    expect(within(card).getByText(/셸로 echo를 한 번 실행하고/)).toBeTruthy();
    const prompt = await within(card).findByRole("region", {
      name: "shellbot(셸 담당)의 승인 요청",
    });
    expect(within(prompt).getByText("echo child-ok")).toBeTruthy();
    expect(within(card).getByText("승인 대기")).toBeTruthy();

    await user.click(within(prompt).getByRole("checkbox", { name: "이 대화에서는 묻지 않기" }));
    await user.click(within(prompt).getByRole("button", { name: "승인" }));

    expect(api.input).toHaveBeenCalledWith(shell.child_run_id, {
      decision: "approve",
      session_auto_approve: true,
    });
    await waitFor(() => expect(store.getState().sessionAutoApprove).toBe(true));
    // 재개된 자식 런이 끝까지 가면 카드도 완료가 된다(부모의 child_run.completed 전이라도).
    expect(await within(card).findByText("완료")).toBeTruthy();
    expect(within(card).queryByRole("region", { name: /승인 요청/ })).toBeNull();
  });

  it("대기열에 있는 자식은 몇 번째인지 보여 주고 중지 버튼을 둔다", async () => {
    const queued: SessionDetail = {
      ...o.childShell.detailWaiting,
      active_run: {
        ...o.childShell.detailWaiting.active_run!,
        status: "queued",
        pending_interaction: null,
      },
    };
    const first = o.childShell.eventsWaiting[0];
    const queuedEvent = { ...first, type: "run.queued", data: { position: 2 } } as DataRunEvent;
    const api = fakeApi({
      details: { [shell.child_session_id]: queued },
      streams: { [`${shell.child_session_id}:0`]: { events: [queuedEvent], open: true } },
    });
    renderCards(parentStore(o.send.slice(0, firstChild + 1)), api, [shell.tool_call_id]);
    const card = screen.getByRole("region", { name: "하위 에이전트 셸 담당" });
    expect(await within(card).findByText("대기열 2번째")).toBeTruthy();
    expect(within(card).getByRole("button", { name: "셸 담당 중지" })).toBeTruthy();
  });

  it("끝난 호출은 결과 파일 링크와 JSON 결과를 보여 주고, 초안으로 돈 하위 에이전트에 초안 배지를 붙인다", async () => {
    const api = fakeApi({
      details: {
        [shell.child_session_id]: o.childShell.detailDone,
        [extract.child_session_id]: o.childJson.detailDone,
      },
      streams: {},
    });
    renderCards(parentStore(o.send), api, [shell.tool_call_id, extract.tool_call_id]);

    const shellCard = screen.getByRole("region", { name: "하위 에이전트 셸 담당" });
    expect(within(shellCard).getByText("완료")).toBeTruthy();
    expect(within(shellCard).queryByText("초안")).toBeNull();
    const link = await within(shellCard).findByRole("link", { name: "outputs/report.md 내려받기" });
    const file = (o.childShell.detailDone.last_run!.output!.files as { id: string }[])[0];
    expect(link.getAttribute("href")).toBe(`/files/${shell.child_session_id}/${file.id}`);
    expect(within(shellCard).getAllByText("스텝 3").length).toBeGreaterThan(0);

    const jsonCard = screen.getByRole("region", { name: "하위 에이전트 추출 담당" });
    expect(within(jsonCard).getByText("초안")).toBeTruthy();
    expect(await within(jsonCard).findByText("JSON 결과")).toBeTruthy();
    expect(within(jsonCard).getByText('"분기 보고서"')).toBeTruthy();
    expect(within(jsonCard).getByText("3")).toBeTruthy();
    // 끝난 카드는 자식 스트림을 열지 않는다.
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("새로 고친 뒤 기록에서 그린 카드는 부모 런의 이벤트를 다시 받아 자식 세션을 찾고, 펼치면 자식 대화를 그린다", async () => {
    const store = createRunStore();
    store.getState().actions.reset(historyState(o.parentMessagesDone.items, o.parentDetailDone));
    expect(store.getState().toolCalls[shell.tool_call_id].child).toBeNull();
    const api = fakeApi({
      details: { [shell.child_session_id]: o.childShell.detailDone },
      messages: { [shell.child_session_id]: o.childShell.messagesDone },
      streams: { [`${parentSessionId}:0`]: { events: o.send } },
    });
    const { user } = renderCards(store, api, [shell.tool_call_id]);

    const card = await screen.findByRole("region", { name: "하위 에이전트 셸 담당" });
    expect(
      await within(card).findByRole("link", { name: "outputs/report.md 내려받기" }),
    ).toBeTruthy();
    const replay = new URL(api.fetch.mock.calls[0][0].url);
    expect(replay.searchParams.get("run_id")).toBe(parentRunId);

    await user.click(within(card).getByRole("button", { name: /셸 담당/ }));
    expect(
      await within(card).findByText("셸 결과: child-ok. 보고서를 outputs/report.md에 썼다."),
    ).toBeTruthy();
    expect(within(card).getByRole("button", { name: /^shell\s*echo child-ok/ })).toBeTruthy();
  });
});
