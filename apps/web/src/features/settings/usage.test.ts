import { describe, expect, it } from "vitest";
import i18n from "@/i18n";
import { buildUsageTable, formatCost, usageRange, type UsageReport } from "./usage";

const t = i18n.getFixedT("ko", "settings");

const empty = {
  runs: 0,
  input_tokens: 0,
  output_tokens: 0,
  reasoning_tokens: 0,
  cached_input_tokens: 0,
  cost_usd: null,
  unpriced_runs: 0,
  estimated: false,
};

function report(groupBy: UsageReport["group_by"], items: UsageReport["items"]): UsageReport {
  const total = items.reduce(
    (sum, row) => ({
      runs: sum.runs + row.runs,
      input_tokens: sum.input_tokens + row.input_tokens,
      output_tokens: sum.output_tokens + row.output_tokens,
      reasoning_tokens: sum.reasoning_tokens + row.reasoning_tokens,
      cached_input_tokens: sum.cached_input_tokens + row.cached_input_tokens,
      cost_usd:
        row.cost_usd === null || row.cost_usd === undefined
          ? sum.cost_usd
          : (sum.cost_usd ?? 0) + row.cost_usd,
      unpriced_runs: sum.unpriced_runs + row.unpriced_runs,
      estimated: sum.estimated || row.estimated,
    }),
    { ...empty } as UsageReport["total"],
  );
  return {
    group_by: groupBy,
    since: "2026-10-01T00:00:00Z",
    until: "2026-11-01T00:00:00Z",
    tz_offset_minutes: 540,
    items,
    total,
  };
}

describe("사용량 표 서식", () => {
  it("날짜는 요일과 함께, 숫자는 천 단위로 보인다", () => {
    const table = buildUsageTable(
      report("day", [
        {
          ...empty,
          key: "2026-10-12",
          label: null,
          deleted: false,
          runs: 3,
          input_tokens: 12_345,
          output_tokens: 655,
        },
        {
          ...empty,
          key: "2026-10-13",
          label: null,
          deleted: false,
          runs: 1,
          input_tokens: 1000,
          output_tokens: 0,
        },
      ]),
      t,
    );

    expect(table.rows.map((row) => row.label)).toEqual(["10월 12일 (월)", "10월 13일 (화)"]);
    expect(table.rows[0]).toMatchObject({ runs: "3", inputTokens: "12,345", outputTokens: "655" });
    expect(table.total).toMatchObject({ label: "합계", runs: "4", inputTokens: "13,345" });
    expect(table.rows[0].share).toBeCloseTo(13_000 / 14_000);
    expect(table.hasCost).toBe(false);
    expect(table.rows[0].cost).toBe("-");
  });

  it("에이전트·모델 이름이 없거나 지워졌으면 그렇게 적는다", () => {
    const agents = buildUsageTable(
      report("agent", [
        { ...empty, key: "a1", label: "정리 담당", deleted: false, runs: 1 },
        { ...empty, key: "a2", label: "옛 에이전트", deleted: true, runs: 1 },
      ]),
      t,
    );
    expect(agents.rows.map((row) => row.label)).toEqual(["정리 담당", "옛 에이전트 (삭제됨)"]);
    expect(agents.rows[1].deleted).toBe(true);

    const models = buildUsageTable(
      report("model", [
        { ...empty, key: "gemini-3.8-flash", label: null, deleted: false, runs: 2 },
        { ...empty, key: null, label: null, deleted: false, runs: 1 },
      ]),
      t,
    );
    expect(models.rows.map((row) => row.label)).toEqual(["gemini-3.8-flash", "알 수 없음"]);
    expect(new Set(models.rows.map((row) => row.key)).size).toBe(2);
  });

  it("워크플로우로 묶으면 에이전트 런이, 에이전트로 묶으면 워크플로우 런이 키 없는 한 행이다", () => {
    const workflows = buildUsageTable(
      report("workflow", [
        { ...empty, key: "w1", label: "요약 흐름", deleted: false, runs: 2 },
        { ...empty, key: "w2", label: "옛 흐름", deleted: true, runs: 1 },
        { ...empty, key: null, label: null, deleted: false, runs: 4 },
      ]),
      t,
    );
    expect(workflows.rows.map((row) => row.label)).toEqual([
      "요약 흐름",
      "옛 흐름 (삭제됨)",
      "에이전트 런",
    ]);

    const agents = buildUsageTable(
      report("agent", [{ ...empty, key: null, label: null, deleted: false, runs: 3 }]),
      t,
    );
    expect(agents.rows.map((row) => row.label)).toEqual(["워크플로우 런"]);
  });

  it("비용은 기록된 것만 더하고, 빠진 런이 있으면 일부라고 표시한다", () => {
    const table = buildUsageTable(
      report("model", [
        {
          ...empty,
          key: "priced",
          label: null,
          deleted: false,
          runs: 2,
          input_tokens: 10,
          cost_usd: 0.0014,
        },
        {
          ...empty,
          key: "free",
          label: null,
          deleted: false,
          runs: 1,
          input_tokens: 5,
          unpriced_runs: 1,
        },
        {
          ...empty,
          key: "mixed",
          label: null,
          deleted: false,
          runs: 2,
          input_tokens: 5,
          cost_usd: 1.5,
          unpriced_runs: 1,
          estimated: true,
        },
      ]),
      t,
    );

    expect(table.rows.map((row) => [row.cost, row.costPartial])).toEqual([
      ["$0.0014", false],
      ["-", false],
      ["$1.50", true],
    ]);
    expect(table.total.cost).toBe("$1.50");
    expect(table.total.costPartial).toBe(true);
    expect(table).toMatchObject({ hasCost: true, unpricedRuns: 2, estimated: true });
    expect(formatCost(undefined)).toBe("-");
  });
});

describe("사용량 기간", () => {
  it("최근 N일은 오늘을 포함한 N일 전체이고 달은 1일부터다", () => {
    const now = new Date(2026, 9, 13, 15, 30);

    const week = usageRange("7d", now);
    expect(new Date(week.from)).toEqual(new Date(2026, 9, 7));
    expect(new Date(week.to)).toEqual(new Date(2026, 9, 14));
    expect(week.tzOffsetMinutes).toBe(-now.getTimezoneOffset());

    const month = usageRange("this_month", now);
    expect(new Date(month.from)).toEqual(new Date(2026, 9, 1));
    expect(new Date(month.to)).toEqual(new Date(2026, 10, 1));

    const january = usageRange("last_month", new Date(2026, 0, 5));
    expect(new Date(january.from)).toEqual(new Date(2025, 11, 1));
    expect(new Date(january.to)).toEqual(new Date(2026, 0, 1));
  });
});
