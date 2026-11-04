// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import type { KbDocumentOut } from "@/api/types";
import "@/features/knowledge/i18n";
import { StatusCell } from "./DocumentTable";
import { RateLimitNotice } from "./RateLimitNotice";

afterEach(cleanup);

const FUTURE = "2099-01-01T08:00:00Z";
const PAST = "2001-01-01T08:00:00Z";

function document(overrides: Partial<KbDocumentOut>): KbDocumentOut {
  return {
    id: "d1",
    kb_id: "kb1",
    position: 1,
    name: "회의록",
    data_source_type: "paste_text",
    filename: "회의록.txt",
    mime: "text/plain",
    size_bytes: 10,
    sha256: "x",
    indexing_status: "completed",
    display_status: "available",
    enabled: true,
    archived: false,
    error: null,
    process_rule: {},
    extract: null,
    total_segments: 10,
    completed_segments: 10,
    word_count: 100,
    tokens: 80,
    hit_count: 0,
    batch: "b1",
    processing_started_at: null,
    parsing_completed_at: null,
    cleaning_completed_at: null,
    splitting_completed_at: null,
    completed_at: null,
    paused_at: null,
    stopped_at: null,
    created_at: "2026-11-03T00:00:00Z",
    updated_at: "2026-11-03T00:00:00Z",
    ...overrides,
  };
}

describe("일일 한도 안내", () => {
  it("KB 화면은 멈춘 문서 수와 다시 시도할 수 있는 때, 저절로 다시 시작하지 않는다는 것을 알린다", () => {
    const { rerender } = render(<RateLimitNotice count={3} retryAfter={FUTURE} />);
    expect(screen.getByText("일일 임베딩 한도로 멈춘 문서가 3개 있습니다.")).toBeTruthy();
    expect(screen.getByText(/이후에 그 문서를 골라 '다시 시도'를 누르세요/)).toBeTruthy();
    expect(screen.getByText(/태평양 자정\(한국 16~17시\)에 풀립니다/)).toBeTruthy();
    rerender(<RateLimitNotice count={3} retryAfter={PAST} />);
    expect(
      screen.getByText("한도가 풀렸습니다. 그 문서를 골라 '다시 시도'를 누르세요."),
    ).toBeTruthy();
    rerender(<RateLimitNotice count={0} retryAfter={null} />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("문서 상태 칸은 진행률과 일일 한도로 멈춘 문서의 해제 시각을 보인다", () => {
    const { rerender } = render(
      <MemoryRouter>
        <StatusCell
          document={document({
            indexing_status: "indexing",
            display_status: "indexing",
            completed_segments: 3,
          })}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("색인 중")).toBeTruthy();
    expect(screen.getByText("임베딩 30%")).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("30");
    rerender(
      <MemoryRouter>
        <StatusCell
          document={document({
            indexing_status: "error",
            display_status: "error",
            error: {
              code: "rate_limited_daily",
              message: "오늘의 임베딩 일일 한도를 다 썼다",
              retryable: true,
              retry_after: FUTURE,
            },
          })}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("오류")).toBeTruthy();
    expect(screen.getByText(/일일 한도로 멈춤 · .* 이후 다시 시도/)).toBeTruthy();
  });
});
