// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IndexingTechnique } from "@/api/types";
import "@/features/knowledge/i18n";
import {
  DEFAULT_RETRIEVAL,
  toRetrievalForm,
  type RetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { RetrievalSettingsForm } from "./RetrievalSettingsForm";

afterEach(cleanup);

function Harness({
  technique,
  initial,
  onChange,
}: {
  technique: IndexingTechnique;
  initial: RetrievalForm;
  onChange?: (value: RetrievalForm) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <RetrievalSettingsForm
      value={value}
      technique={technique}
      topKMax={20}
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

describe("RetrievalSettingsForm", () => {
  it("경제 KB는 전문 검색만 고를 수 있고 문턱·결합 칸이 없다", () => {
    render(
      <Harness
        technique="economy"
        initial={toRetrievalForm({ ...DEFAULT_RETRIEVAL, search_method: "full_text_search" })}
      />,
    );
    expect((screen.getByRole("radio", { name: /하이브리드/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByRole("radio", { name: /^벡터/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("경제 지식 베이스는 전문 검색만 쓸 수 있습니다.")).toBeTruthy();
    expect(screen.queryByText("코사인 문턱")).toBeNull();
    expect(screen.queryByText("결합 방식")).toBeNull();
  });

  it("문턱은 켜야 값을 넣을 수 있고, Top K 상한을 넘으면 알린다", async () => {
    const onChange = vi.fn();
    render(
      <Harness
        technique="high_quality"
        initial={toRetrievalForm(DEFAULT_RETRIEVAL)}
        onChange={onChange}
      />,
    );
    const user = userEvent.setup();
    const threshold = screen.getByRole("spinbutton", { name: "코사인 문턱" }) as HTMLInputElement;
    expect(threshold.disabled).toBe(true);
    await user.click(screen.getByRole("switch", { name: "코사인 문턱" }));
    expect(threshold.disabled).toBe(false);
    const topK = screen.getByRole("spinbutton", { name: /가져올 수/ });
    await user.clear(topK);
    await user.type(topK, "25");
    expect(screen.getByText("1 이상 상한 이하의 정수여야 합니다.")).toBeTruthy();
    expect(onChange.mock.calls.at(-1)?.[0].top_k).toBe(25);
  });

  it("벡터 검색으로 바꾸면 가중치·결합 칸을 숨긴다", async () => {
    render(<Harness technique="high_quality" initial={toRetrievalForm(DEFAULT_RETRIEVAL)} />);
    expect(screen.getByRole("slider", { name: "벡터 가중치" })).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("radio", { name: /^벡터/ }));
    expect(screen.queryByRole("slider", { name: "벡터 가중치" })).toBeNull();
  });
});
