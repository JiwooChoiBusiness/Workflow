import { describe, expect, it } from "vitest";
import {
  DEFAULT_LOG_FILTERS,
  clearFilters,
  hasActiveFilters,
  logFiltersToParams,
  parseLogFilters,
  periodSince,
  runListQuery,
  sessionListQuery,
  type LogFilters,
} from "./filters";

const NOW = Date.UTC(2026, 9, 13, 12, 0, 0);

describe("로그 필터 상태", () => {
  it("주소의 값을 읽고 모르는 값은 기본값으로 둔다", () => {
    const filters = parseLogFilters(
      new URLSearchParams("tab=runs&agent=a1&source=api&period=7d&status=failed"),
    );
    expect(filters).toEqual({
      tab: "runs",
      agent: "a1",
      source: "api",
      period: "7d",
      status: "failed",
    });

    const junk = parseLogFilters(
      new URLSearchParams("tab=x&agent=%20&source=mail&period=2y&status=done"),
    );
    expect(junk).toEqual(DEFAULT_LOG_FILTERS);
  });

  it("기본값은 주소에 남기지 않고 다시 읽으면 같은 상태가 된다", () => {
    expect(logFiltersToParams(DEFAULT_LOG_FILTERS).toString()).toBe("");
    const filters: LogFilters = {
      tab: "runs",
      agent: "a1",
      source: "webapp",
      period: "30d",
      status: "waiting_input",
    };
    const params = logFiltersToParams(filters);
    expect(params.toString()).toBe(
      "tab=runs&agent=a1&source=webapp&period=30d&status=waiting_input",
    );
    expect(parseLogFilters(params)).toEqual(filters);
  });

  it("기간은 지금 기준 시작 시각으로 바뀐다", () => {
    expect(periodSince("all", NOW)).toBeUndefined();
    expect(periodSince("1d", NOW)).toBe("2026-10-12T12:00:00.000Z");
    expect(periodSince("90d", NOW)).toBe("2026-07-15T12:00:00.000Z");
  });

  it("상태 필터는 런 목록에만 들어간다", () => {
    const filters: LogFilters = {
      tab: "sessions",
      agent: "a1",
      source: "preview",
      period: "7d",
      status: "failed",
    };
    expect(sessionListQuery(filters, NOW)).toEqual({
      agent_id: "a1",
      source: "preview",
      since: "2026-10-06T12:00:00.000Z",
    });
    expect(runListQuery(filters, NOW)).toEqual({
      agent_id: "a1",
      source: "preview",
      since: "2026-10-06T12:00:00.000Z",
      status: "failed",
    });
    expect(runListQuery(DEFAULT_LOG_FILTERS, NOW)).toEqual({});
  });

  it("에이전트 로그 화면에서는 에이전트를 빼고 필터 여부를 본다", () => {
    const agentOnly: LogFilters = { ...DEFAULT_LOG_FILTERS, agent: "a1" };
    expect(hasActiveFilters(agentOnly)).toBe(true);
    expect(hasActiveFilters(agentOnly, { ignoreAgent: true })).toBe(false);
    // 세션 탭에서는 상태가 걸려 있어도 쓰이지 않으므로 필터로 치지 않는다.
    expect(hasActiveFilters({ ...DEFAULT_LOG_FILTERS, status: "failed" })).toBe(false);
    expect(hasActiveFilters({ ...DEFAULT_LOG_FILTERS, tab: "runs", status: "failed" })).toBe(true);

    const busy: LogFilters = {
      tab: "runs",
      agent: "a1",
      source: "api",
      period: "7d",
      status: "failed",
    };
    expect(clearFilters(busy)).toEqual({ ...DEFAULT_LOG_FILTERS, tab: "runs" });
    expect(clearFilters(busy, { keepAgent: true })).toEqual({
      ...DEFAULT_LOG_FILTERS,
      tab: "runs",
      agent: "a1",
    });
  });
});
