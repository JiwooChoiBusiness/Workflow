import { describe, expect, it } from "vitest";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  parseWfRunFilters,
  shortRunId,
  wfRunFiltersToParams,
  wfRunListQuery,
} from "./runLogFilters";

const NOW = Date.UTC(2026, 9, 14, 12, 0, 0);

describe("워크플로우 런 목록 필터", () => {
  it("주소에서 읽고 모르는 값은 기본값으로 둔다", () => {
    expect(
      parseWfRunFilters(
        new URLSearchParams("workflow=w1&trigger=webapp&status=failed&period=7d&tab=workflows"),
      ),
    ).toEqual({ workflow: "w1", trigger: "webapp", status: "failed", period: "7d" });
    expect(
      parseWfRunFilters(new URLSearchParams("trigger=resume&status=done&period=1y&workflow=%20")),
    ).toEqual(DEFAULT_WF_RUN_FILTERS);
  });

  it("다른 매개변수(탭)는 두고 필터만 바꾸며 기본값은 남기지 않는다", () => {
    const base = new URLSearchParams("tab=workflows&trigger=api&status=failed");
    const params = wfRunFiltersToParams({ ...DEFAULT_WF_RUN_FILTERS, workflow: "w2" }, base);
    expect(params.toString()).toBe("tab=workflows&workflow=w2");
    expect(hasWfRunFilters(parseWfRunFilters(params))).toBe(true);
    expect(hasWfRunFilters(parseWfRunFilters(params), { ignoreWorkflow: true })).toBe(false);
  });

  it("서버 목록 질의로 바꾼다. 기간은 지금 기준 시작 시각이다", () => {
    expect(
      wfRunListQuery(
        { workflow: "w1", trigger: "single_node", status: "succeeded", period: "1d" },
        NOW,
      ),
    ).toEqual({ trigger: "single_node", status: "succeeded", since: "2026-10-13T12:00:00.000Z" });
    expect(wfRunListQuery(DEFAULT_WF_RUN_FILTERS, NOW)).toEqual({});
  });

  it("짧은 런 id는 시각이 아닌 끝부분이다", () => {
    expect(shortRunId("01a13962-f533-7e6d-8034-c1a7b5a78656")).toBe("b5a78656");
  });
});
