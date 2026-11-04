// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeBaseOut } from "@/api/types";
import "@/features/knowledge/i18n";
import { DEFAULT_RETRIEVAL } from "@/features/knowledge/retrievalSettings";
import { KbSettingsPanel } from "./KbSettingsPanel";

const mutate = vi.fn();

vi.mock("@/features/knowledge/api", () => ({
  usePatchKnowledgeBase: () => ({ mutate, isPending: false }),
  useDeleteKnowledgeBase: () => ({ mutate: vi.fn(), isPending: false }),
}));

// jsdom에는 Radix Select가 여는 동안 부르는 포인터 캡처·스크롤·크기 관찰 API가 없다.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});
afterEach(cleanup);
beforeEach(() => mutate.mockReset());

function kb(overrides: Partial<KnowledgeBaseOut> = {}): KnowledgeBaseOut {
  return {
    id: "kb1",
    slug: "manual",
    name: "매뉴얼",
    icon: null,
    description: "",
    doc_form: "text_model",
    indexing_technique: "high_quality",
    embedding: {
      model: "gemini-embedding-2",
      dimensions: 768,
      prefix: "gemini2",
      task_type: null,
      version: 1,
    },
    process_rule: {},
    retrieval_model: DEFAULT_RETRIEVAL,
    search_while_indexing: true,
    built_in_fields_enabled: false,
    data_policy: { key_tier: "free", acknowledged_at: "2026-11-03T00:00:00Z" },
    document_count: 2,
    segment_count: 40,
    indexing_document_count: 0,
    embedded_tokens_estimated: 1000,
    created_at: "2026-11-03T00:00:00Z",
    updated_at: "2026-11-03T00:00:00Z",
    ...overrides,
  };
}

function show(base: KnowledgeBaseOut) {
  render(
    <MemoryRouter>
      <KbSettingsPanel kb={base} stats={undefined} defaults={undefined} />
    </MemoryRouter>,
  );
  return userEvent.setup();
}

describe("KbSettingsPanel", () => {
  it("경제 KB는 확인 창을 거쳐 고품질로 올린다", async () => {
    const user = show(
      kb({
        indexing_technique: "economy",
        embedding: null,
        retrieval_model: { ...DEFAULT_RETRIEVAL, search_method: "full_text_search" },
      }),
    );
    await user.click(screen.getByRole("button", { name: "고품질로 올리기" }));
    expect(await screen.findByRole("alertdialog", { name: "고품질로 올리기" })).toBeTruthy();
    expect(screen.getByText(/경제로 되돌릴 수 없습니다/)).toBeTruthy();
    const buttons = screen.getAllByRole("button", { name: "고품질로 올리기" });
    await user.click(buttons[buttons.length - 1]);
    expect(mutate.mock.calls[0][0]).toEqual({ indexing_technique: "high_quality" });
  });

  it("이름을 바꾼 칸만 저장하고, 키 등급은 안내를 다시 확인해야 저장한다", async () => {
    const user = show(kb());
    const name = screen.getByRole("textbox", { name: "이름" });
    await user.clear(name);
    await user.type(name, "새 이름");
    const saves = screen.getAllByRole("button", { name: "저장" });
    await user.click(saves[0]);
    expect(mutate.mock.calls[0][0]).toEqual({ name: "새 이름", description: "" });
    // 유료 키가 없으면 유료 키 전용은 고를 수 없어 데이터 정책의 저장 단추는 계속 막혀 있다.
    expect(
      (screen.getByRole("radio", { name: /유료 키 전용/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
    const policySave = saves[saves.length - 1] as HTMLButtonElement;
    expect(policySave.disabled).toBe(true);
  });
});

describe("KbSettingsPanel 임베딩 설정", () => {
  it("차원을 바꾸면 저장 전에 재임베딩 확인 창을 거친다", async () => {
    const user = show(kb());
    const heading = screen.getByRole("heading", { name: "색인" });
    const panel = within(heading.closest("section") as HTMLElement);
    const save = panel.getByRole("button", { name: "저장" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(panel.getByText("gemini-embedding-2")).toBeTruthy();

    await user.click(panel.getByRole("combobox", { name: "차원" }));
    await user.click(await screen.findByRole("option", { name: "1536 (저장 공간 2배)" }));
    expect(save.disabled).toBe(false);
    await user.click(save);

    const dialog = await screen.findByRole("alertdialog", { name: "모든 청크를 다시 임베딩" });
    expect(mutate).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "다시 임베딩" }));
    expect(mutate.mock.calls[0][0]).toEqual({
      embedding: { model: "gemini-embedding-2", dimensions: 1536, prefix: "gemini2" },
      confirm_reembed: true,
    });
  });
});
