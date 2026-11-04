// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RetrieveRecord } from "@/api/types";
import "@/features/knowledge/i18n";
import { ResultCard } from "./ResultCard";

afterEach(cleanup);

function record(overrides: Partial<RetrieveRecord> = {}): RetrieveRecord {
  return {
    segment: {
      id: "s1",
      position: 3,
      document_id: "d1",
      content: "연차는 다음 해 3월까지 이월할 수 있다.",
      answer: null,
      word_count: 20,
      tokens: 15,
      hit_count: 0,
      enabled: true,
      status: "completed",
      document: {
        id: "d1",
        data_source_type: "upload_file",
        name: "인사 규정",
        indexing_status: "completed",
      },
    },
    score: 0.8123,
    fusion: {
      cosine: 0.7911,
      vector_score: 1,
      keyword_score: 0.375,
      vector_rank: 1,
      keyword_rank: 2,
    },
    ...overrides,
  };
}

function show(props: Partial<Parameters<typeof ResultCard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <ResultCard
        kbId="kb1"
        record={record()}
        position={1}
        settings={{ search_method: "hybrid_search", reranking_mode: "weighted_score" }}
        technique="high_quality"
        warnings={[]}
        barScale={1}
        {...props}
      />
    </MemoryRouter>,
  );
}

describe("ResultCard", () => {
  it("하이브리드는 결합 점수와 결합 내역(코사인 원값·벡터·키워드·순위)을 보인다", () => {
    show();
    const card = screen.getByRole("article", { name: "#1" });
    expect(within(card).getByText("0.812")).toBeTruthy();
    expect(within(card).getByText("결합 점수")).toBeTruthy();
    const breakdown = within(card).getByLabelText("결합 내역");
    expect(within(breakdown).getByText("0.791")).toBeTruthy();
    expect(within(breakdown).getByText("0.375")).toBeTruthy();
    expect(within(breakdown).getByText("순위 벡터 1 · 전문 2")).toBeTruthy();
    expect(within(card).getByRole("link", { name: "인사 규정" }).getAttribute("href")).toBe(
      "/knowledge/kb1/documents/d1",
    );
  });

  it("벡터 검색은 코사인 원값, 질의 임베딩 실패로 전문 검색만 돈 결과는 bm25 점수다", () => {
    const { unmount } = show({
      settings: { search_method: "semantic_search", reranking_mode: "weighted_score" },
      record: record({ score: 0.7911 }),
    });
    expect(screen.getByText("코사인 원값", { selector: "span" })).toBeTruthy();
    unmount();
    show({
      record: record({
        score: 1,
        fusion: { cosine: null, vector_score: null, keyword_score: 1, keyword_rank: 1 },
      }),
      warnings: [{ code: "vector_unavailable", message: "x" }],
    });
    expect(screen.getByText("bm25 점수")).toBeTruthy();
  });

  it("색인 중인 문서의 결과에 표시를 달고, 정답 등록은 청크·문서 중에 고른다", async () => {
    const onMark = vi.fn();
    const base = record();
    show({
      onMark,
      record: {
        ...base,
        segment: {
          ...base.segment,
          document: { ...base.segment.document, indexing_status: "indexing" },
        },
      },
    });
    expect(screen.getByText("색인 중인 문서")).toBeTruthy();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "정답으로 등록" }));
    await user.click(await screen.findByRole("menuitem", { name: "이 문서를 정답으로" }));
    expect(onMark).toHaveBeenCalledWith("document");
  });
});
