// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { RunEvent } from "@/api/run-events";
import { historyState } from "@/features/sessions/history";
import type { MessageOut, SessionDetail } from "@/features/sessions/types";
import { runCitations, type Citation } from "@/lib/citations";
import { applyRunEvents, createRunStore, initialRunState } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { CitationChips } from "./CitationChips";
import { citationAnchors, MessageList } from "./MessageList";

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});
afterEach(cleanup);

function cite(position: number, overrides: Partial<Citation> = {}): Citation {
  return {
    position,
    dataset_id: "kb-1",
    dataset_name: "사내 규정",
    document_id: `doc-${position}`,
    document_name: `문서 ${position}.md`,
    segment_id: `seg-${position}`,
    segment_position: position * 2,
    score: 0.5 + position / 10,
    content: `${position}번 청크 내용`,
    ...overrides,
  };
}

describe("CitationChips", () => {
  it("칩은 position 순서로 놓인다(답의 [n]과 같은 번호)", () => {
    render(<CitationChips citations={[cite(3), cite(1), cite(2)]} />);
    const chips = within(screen.getByRole("list", { name: "인용" })).getAllByRole("button");
    expect(chips.map((chip) => chip.getAttribute("aria-label"))).toEqual([
      "인용 1: 문서 1.md",
      "인용 2: 문서 2.md",
      "인용 3: 문서 3.md",
    ]);
  });

  it("팝오버에 문서·지식 베이스·점수·내용과 콘솔이면 청크 화면 링크를 보인다", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <CitationChips citations={[cite(2, { answer: "다음 해로 넘긴다" })]} chunkLinks />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: "인용 2: 문서 2.md" }));
    const popover = await screen.findByRole("dialog", { name: "인용 2 내용" });
    expect(within(popover).getByText("사내 규정 · 청크 #4", { exact: false })).toBeTruthy();
    expect(within(popover).getByText("점수 0.70")).toBeTruthy();
    expect(within(popover).getByText(/2번 청크 내용/)).toBeTruthy();
    expect(within(popover).getByText(/다음 해로 넘긴다/)).toBeTruthy();
    expect(
      within(popover).getByRole("link", { name: "청크 화면에서 전문 보기" }).getAttribute("href"),
    ).toBe("/knowledge/kb-1/documents/doc-2");
  });

  it("맥락 확장 문서는 팝오버에 맞은 작은 조각을 보인다", async () => {
    const user = userEvent.setup();
    const child = { id: "child-1", content: "이월은 5일까지", position: 2, score: 0.91 };
    render(<CitationChips citations={[cite(1, { child_chunks: [child] })]} />);
    await user.click(screen.getByRole("button", { name: "인용 1: 문서 1.md" }));
    const popover = await screen.findByRole("dialog", { name: "인용 1 내용" });
    // 작은 조각이 있는 인용의 청크는 큰 조각이다.
    expect(within(popover).getByText("사내 규정 · 큰 조각 #2", { exact: false })).toBeTruthy();
    expect(within(popover).getByText("맞은 작은 조각")).toBeTruthy();
    expect(within(popover).getByText("작은 조각 #2")).toBeTruthy();
    expect(within(popover).getByText(/이월은 5일까지/)).toBeTruthy();
  });

  it("웹앱(링크 없음)에서는 청크 화면 링크를 두지 않는다", async () => {
    const user = userEvent.setup();
    render(<CitationChips citations={[cite(1)]} />);
    await user.click(screen.getByRole("button", { name: "인용 1: 문서 1.md" }));
    const popover = await screen.findByRole("dialog", { name: "인용 1 내용" });
    expect(within(popover).queryByRole("link")).toBeNull();
  });
});

function textItem(id: string, runId: string, text: string) {
  return { kind: "text" as const, id, runId, messageId: id, step: 0, text, done: true };
}

describe("MessageList의 인용 칩", () => {
  const items = [
    { kind: "user" as const, id: "u1", text: "질문" },
    textItem("t1", "run-1", "검색해 보겠습니다."),
    textItem("t2", "run-1", "연차는 5일까지 넘길 수 있습니다 [1]."),
  ];

  function show(hideCitations: boolean) {
    const store = createRunStore({ initial: { items, citations: { "run-1": [cite(1)] } } });
    render(
      <MemoryRouter>
        <RunStoreProvider store={store}>
          <MessageList renderInteraction={() => null} hideCitations={hideCitations} />
        </RunStoreProvider>
      </MemoryRouter>,
    );
  }

  it("런의 마지막 답 아래에만 붙는다", () => {
    expect([...citationAnchors(items, { "run-1": [cite(1)] })]).toEqual([["t2", "run-1"]]);
    show(false);
    expect(screen.getAllByRole("list", { name: "인용" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "인용 1: 문서 1.md" })).toBeTruthy();
  });

  it("웹앱 설정으로 끄면 보이지 않는다", () => {
    show(true);
    expect(screen.queryByRole("list", { name: "인용" })).toBeNull();
  });
});

function message(id: string, runId: string, resources: Citation[]): MessageOut {
  return {
    id,
    session_id: "s1",
    run_id: runId,
    seq: 0,
    role: "assistant",
    text: "",
    blocks: [{ type: "text", text: `답 ${id}` }],
    stop_reason: "end_turn",
    files: [],
    usage: null,
    retriever_resources: resources,
    created_at: "2026-11-03T00:00:00Z",
  } as unknown as MessageOut;
}

describe("에이전트 인용은 런의 마지막 assistant 메시지 것만", () => {
  it("중간 스텝 메시지의 인용은 버린다", () => {
    const messages = [
      message("m1", "run-1", [cite(1)]),
      message("m2", "run-1", [cite(1), cite(2)]),
      message("m3", "run-2", []),
    ];
    expect(runCitations(messages)).toEqual({ "run-1": [cite(1), cite(2)] });
  });

  it("진행 중인 런의 인용은 기록이 아니라 이벤트 재생으로 쌓는다", () => {
    const messages = [message("m1", "run-1", [cite(1)]), message("m2", "run-2", [cite(2)])];
    const detail = {
      id: "s1",
      active_run: { id: "run-2", status: "waiting_input", approval_mode: "off" },
      last_run: null,
    } as unknown as SessionDetail;
    expect(historyState(messages, detail).citations).toEqual({ "run-1": [cite(1)] });
  });

  it("스트리밍 중에는 search_knowledge 결과의 인용을 쌓고 끝나면 저장본으로 바꾼다", () => {
    const base = { run_id: "run-1", session_id: "s1", ts: "2026-11-03T00:00:00Z" };
    const completed = (seq: number, id: string, resources: Citation[]) =>
      ({
        ...base,
        seq,
        type: "tool_call.completed",
        data: {
          tool_call_id: id,
          status: "ok",
          duration_ms: 5,
          output_excerpt: "",
          output_bytes: 0,
          retriever_resources: resources,
        },
      }) as unknown as RunEvent;
    const state = applyRunEvents(initialRunState(), [
      completed(1, "call-a", [cite(2)]),
      completed(2, "call-b", [cite(2), cite(1)]),
    ]);
    expect(state.citations["run-1"]?.map((item) => item.position)).toEqual([1, 2]);
    expect(state.toolCalls["call-b"]?.retrieverResources).toHaveLength(2);

    const store = createRunStore({ initial: state });
    const stored = [cite(1, { content: "저장본은 2,000자까지" }), cite(2)];
    store.getState().actions.setCitations("run-1", stored);
    expect(store.getState().citations["run-1"]).toEqual(stored);
    store.getState().actions.setCitations("run-1", []);
    expect(store.getState().citations["run-1"]).toBeUndefined();
  });
});
