import { describe, expect, it } from "vitest";
import type { KbDocumentOut } from "@/api/types";
import {
  actionStates,
  batchPollInterval,
  canRetryNow,
  documentError,
  documentsPollInterval,
  isDailyLimited,
  isInProgress,
  POLL_MS,
  progressPercent,
  relativeBase,
  statusTone,
} from "./documentStatus";

type Doc = Pick<
  KbDocumentOut,
  | "indexing_status"
  | "display_status"
  | "enabled"
  | "archived"
  | "error"
  | "total_segments"
  | "completed_segments"
>;

function doc(overrides: Partial<Doc> = {}): Doc {
  return {
    indexing_status: "completed",
    display_status: "available",
    enabled: true,
    archived: false,
    error: null,
    total_segments: 10,
    completed_segments: 10,
    ...overrides,
  };
}

const daily = {
  code: "rate_limited_daily",
  message: "오늘의 임베딩 일일 한도를 다 썼다",
  retryable: true,
  retry_after: "2026-11-04T08:00:00+00:00",
};

describe("문서 상태", () => {
  it("대기부터 임베딩까지가 진행 중이고, 진행률은 임베딩 단계에서만 센다", () => {
    for (const status of ["waiting", "parsing", "cleaning", "splitting", "indexing"] as const) {
      expect(isInProgress(doc({ indexing_status: status }))).toBe(true);
    }
    for (const status of ["completed", "error", "paused"] as const) {
      expect(isInProgress(doc({ indexing_status: status }))).toBe(false);
    }
    expect(progressPercent(doc({ indexing_status: "indexing", completed_segments: 3 }))).toBe(30);
    expect(
      progressPercent(doc({ indexing_status: "splitting", completed_segments: 3 })),
    ).toBeNull();
    expect(progressPercent(doc({ indexing_status: "indexing", total_segments: 0 }))).toBeNull();
    expect(statusTone("error")).toBe("danger");
    expect(statusTone("available")).toBe("ok");
  });

  it("진행 중 문서가 있거나 KB가 색인 중을 세면 2초마다, 다 끝나면 멈춘다", () => {
    expect(documentsPollInterval([doc(), doc({ indexing_status: "indexing" })])).toBe(POLL_MS);
    expect(documentsPollInterval([doc(), doc({ indexing_status: "error" })])).toBe(false);
    expect(documentsPollInterval([doc()], 2)).toBe(POLL_MS);
    expect(documentsPollInterval(undefined)).toBe(false);
    expect(POLL_MS).toBe(2000);
  });

  it("올린 묶음은 진행 중 문서가 남아 있을 때만 2초마다 읽고, 받기 전이나 다 끝나면 멈춘다", () => {
    expect(batchPollInterval([doc({ indexing_status: "waiting" }), doc()])).toBe(POLL_MS);
    expect(batchPollInterval([doc(), doc({ indexing_status: "error" })])).toBe(false);
    expect(batchPollInterval([doc({ indexing_status: "paused" })])).toBe(false);
    expect(batchPollInterval([])).toBe(false);
    expect(batchPollInterval(undefined)).toBe(false);
  });

  it("일일 한도 오류는 풀리는 시각 전에는 다시 시도할 수 없다", () => {
    const limited = doc({ indexing_status: "error", display_status: "error", error: daily });
    const error = documentError(limited);
    expect(error).toEqual({
      code: "rate_limited_daily",
      message: daily.message,
      retryable: true,
      retryAfter: new Date(daily.retry_after),
    });
    expect(isDailyLimited(limited)).toBe(true);
    const before = new Date("2026-11-04T07:59:00Z").getTime();
    const after = new Date("2026-11-04T08:00:01Z").getTime();
    expect(canRetryNow(limited, before)).toBe(false);
    expect(canRetryNow(limited, after)).toBe(true);
    const parse = doc({
      indexing_status: "error",
      error: { code: "extract_timeout", message: "x", retryable: true },
    });
    expect(canRetryNow(parse, before)).toBe(true);
    expect(isDailyLimited(parse)).toBe(false);
    expect(documentError(doc())).toBeNull();
  });

  it("켜기·끄기·보관은 색인 중 문서가 섞이면 막고, 나머지는 맞는 문서가 있을 때만 연다", () => {
    const busy = actionStates([doc({ enabled: false }), doc({ indexing_status: "indexing" })]);
    expect(busy.enable).toEqual({ enabled: false, reason: "indexing" });
    expect(busy.archive).toEqual({ enabled: false, reason: "indexing" });
    expect(busy.pause).toEqual({ enabled: true });
    expect(busy.resume).toEqual({ enabled: false, reason: "none" });

    const settled = actionStates([
      doc({ enabled: false, display_status: "disabled" }),
      doc({ archived: true, display_status: "archived" }),
      doc({ indexing_status: "paused", display_status: "paused" }),
      doc({ indexing_status: "error", display_status: "error" }),
    ]);
    expect(settled.enable.enabled).toBe(true);
    expect(settled.disable.enabled).toBe(true);
    expect(settled.unarchive.enabled).toBe(true);
    expect(settled.resume.enabled).toBe(true);
    expect(settled.retry.enabled).toBe(true);
    expect(settled.pause).toEqual({ enabled: false, reason: "none" });
  });

  it("상대 시각의 기준은 미래 시각이면 그 시각이라 '지금'으로 보인다", () => {
    const now = Date.UTC(2026, 10, 3, 0, 0, 0);
    expect(relativeBase("2026-11-03T00:00:05Z", now)).toBe(now + 5000);
    expect(relativeBase("2026-11-02T23:59:00Z", now)).toBe(now);
    expect(relativeBase("bad", now)).toBe(now);
  });
});
