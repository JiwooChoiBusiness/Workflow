// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { DocumentPreview } from "@/api/types";
import "@/features/knowledge/i18n";
import { ChunkPreview } from "./ChunkPreview";

afterEach(cleanup);

const preview: DocumentPreview = {
  chunks: [
    { content: "첫 문단", tokens: 12, children: [] },
    { content: "둘째 문단", tokens: 9, children: [] },
  ],
  total_chunks: 120,
  total_child_chunks: 0,
  tokens: 9000,
  estimated_minutes: 2,
  extract: {},
  warnings: ["text_truncated"],
};

describe("ChunkPreview", () => {
  it("청크 수·토큰·예상 소요와 일일 한도를 넣지 않았다는 문구를 보인다", () => {
    render(
      <ChunkPreview
        preview={preview}
        docForm="text_model"
        technique="high_quality"
        rate={80}
        daily={{ remaining: 500, blockedUntil: null }}
      />,
    );
    expect(screen.getByText("청크 120개")).toBeTruthy();
    expect(screen.getByText("예상 소요 약 2분")).toBeTruthy();
    expect(
      screen.getByText(
        "예상 소요는 분당 인제스트 몫(분당 80건)으로 나눈 값입니다. 일일 한도는 넣지 않았습니다.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("오늘 남은 일일 한도 500건")).toBeTruthy();
    expect(screen.queryByText(/한도를 넘는 유닛은/)).toBeNull();
    expect(screen.getByText("글이 너무 길어 앞부분만 읽었습니다.")).toBeTruthy();
    expect(screen.getByText("앞 2개를 보여 줍니다.")).toBeTruthy();
  });

  it("유닛이 오늘 남은 한도보다 많으면 경고하고, 막혀 있으면 풀리는 시각을 보인다", () => {
    const { rerender } = render(
      <ChunkPreview
        preview={preview}
        docForm="text_model"
        technique="high_quality"
        rate={80}
        daily={{ remaining: 100, blockedUntil: null }}
      />,
    );
    expect(screen.getByText(/한도를 넘는 유닛은 오늘 벡터가 생기지 않고/)).toBeTruthy();
    rerender(
      <ChunkPreview
        preview={preview}
        docForm="text_model"
        technique="high_quality"
        rate={80}
        daily={{ remaining: 0, blockedUntil: "2026-11-04T08:00:00Z" }}
      />,
    );
    expect(screen.getByText(/오늘은 임베딩을 더 보낼 수 없습니다/)).toBeTruthy();
  });

  it("경제 방식은 임베딩하지 않아 소요와 한도를 말하지 않는다", () => {
    render(
      <ChunkPreview
        preview={{ ...preview, estimated_minutes: 0 }}
        docForm="text_model"
        technique="economy"
        rate={80}
        daily={{ remaining: 0, blockedUntil: null }}
      />,
    );
    expect(screen.getByText("예상 소요 없음(임베딩하지 않음)")).toBeTruthy();
    expect(screen.queryByText(/일일 한도는 넣지 않았습니다/)).toBeNull();
    expect(screen.queryByText(/한도를 넘는 유닛은/)).toBeNull();
  });
});
