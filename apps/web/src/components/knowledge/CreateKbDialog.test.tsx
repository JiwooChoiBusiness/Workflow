// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeDefaults } from "@/api/types";
import "@/features/knowledge/i18n";
import { CreateKbDialog } from "./CreateKbDialog";

const mutateAsync = vi.fn();

vi.mock("@/features/knowledge/api", () => ({
  useCreateKnowledgeBase: () => ({ mutateAsync, isPending: false }),
}));

// Radix 체크박스가 폼 안에서 크기를 재려고 ResizeObserver를 쓴다. jsdom에는 없다.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(cleanup);
beforeEach(() => {
  mutateAsync.mockReset();
  mutateAsync.mockResolvedValue({ id: "kb1" });
});

function defaults(paidKey: boolean): KnowledgeDefaults {
  const limits = { rpm: 90, reserve: 10, tpm: 0, rpd: 1000 };
  return {
    embedding_models: [
      {
        id: "gemini-embedding-2",
        dimensions: [768, 1536, 3072],
        default_dimensions: 768,
        prefixes: ["none", "gemini2"],
        default_prefix: "gemini2",
        input_token_limit: 8192,
      },
    ],
    embedding: { model: "gemini-embedding-2", dimensions: 768, prefix: "gemini2" },
    process_rule: {},
    retrieval_model: {
      search_method: "hybrid_search",
      top_k: 4,
      score_threshold_enabled: false,
      score_threshold: 0.5,
      reranking_mode: "weighted_score",
    },
    limiter: { model: "gemini-embedding-2", capped: true, free: limits, paid: limits },
    daily: [],
    paid_key: paidKey,
    sqlite_version: "3.45.3",
    fts_mode: "contentless",
    index_available: true,
    top_k_max: 20,
    upload_max_files: 20,
    upload_max_mb: 50,
    doc_max_text_mb: 8,
    query_log_days: 90,
  };
}

function setup(paidKey = false) {
  render(
    <MemoryRouter>
      <CreateKbDialog open onOpenChange={() => undefined} defaults={defaults(paidKey)} />
    </MemoryRouter>,
  );
  return userEvent.setup();
}

describe("CreateKbDialog", () => {
  it("데이터 정책을 확인하지 않으면 만들지 않는다", async () => {
    const user = setup();
    await user.type(screen.getByRole("textbox", { name: "이름" }), "사내 문서");
    await user.click(screen.getByRole("button", { name: "만들기" }));
    expect(screen.getByText("데이터 정책 안내를 확인해야 저장할 수 있습니다.")).toBeTruthy();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("유료 키가 없으면 유료 키 전용을 고를 수 없고 그 이유를 보인다", () => {
    setup(false);
    const paid = screen.getByRole("radio", { name: /유료 키 전용/ });
    expect((paid as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/GEMINI_PAID_API_KEY가 없어 고를 수 없습니다/)).toBeTruthy();
  });

  it("확인하면 고른 청크 모드·색인 방식·차원·키 등급으로 만든다", async () => {
    const user = setup(true);
    await user.type(screen.getByRole("textbox", { name: "이름" }), "  매뉴얼  ");
    await user.click(screen.getByRole("radio", { name: /맥락 확장/ }));
    await user.click(screen.getByRole("radio", { name: /유료 키 전용/ }));
    await user.click(screen.getByRole("checkbox", { name: /위 안내를 읽었고/ }));
    await user.click(screen.getByRole("button", { name: "만들기" }));
    expect(mutateAsync).toHaveBeenCalledWith({
      name: "매뉴얼",
      description: "",
      doc_form: "hierarchical_model",
      indexing_technique: "high_quality",
      embedding: { model: "gemini-embedding-2", dimensions: 768, prefix: "gemini2" },
      search_while_indexing: true,
      data_policy: { key_tier: "paid", acknowledged: true },
    });
  });

  it("경제 방식은 임베딩 설정을 보내지 않는다", async () => {
    const user = setup();
    await user.type(screen.getByRole("textbox", { name: "이름" }), "메모");
    await user.click(screen.getByRole("radio", { name: /경제/ }));
    expect(screen.queryByText("임베딩 모델")).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /위 안내를 읽었고/ }));
    await user.click(screen.getByRole("button", { name: "만들기" }));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      indexing_technique: "economy",
      embedding: null,
    });
  });
});
