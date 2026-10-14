import type { TFunction } from "i18next";
import type { components } from "@/api/schema";
import { formatNumber, formatUsd } from "@/lib/format";

// 설정 > 사용량 화면(GET /stats/usage)의 기간 계산과 표 서식.
type Schemas = components["schemas"];
export type UsageReport = Schemas["UsageReport"];
export type UsageRow = Schemas["UsageRow"];
export type UsageTotals = Schemas["UsageTotals"];
export type UsageGroupBy = UsageReport["group_by"];

export const USAGE_GROUPS: readonly UsageGroupBy[] = ["day", "agent", "workflow", "model"];
export const USAGE_PERIODS = ["7d", "30d", "90d", "this_month", "last_month"] as const;
export type UsagePeriod = (typeof USAGE_PERIODS)[number];

export interface UsageQuery {
  groupBy: UsageGroupBy;
  period: UsagePeriod;
}

export interface UsageRange {
  from: string;
  to: string;
  // 날짜로 묶을 때 서버가 쓸 시간대. 화면이 보는 날짜와 같게 브라우저 시간대를 보낸다.
  tzOffsetMinutes: number;
}

const RECENT_DAYS: Partial<Record<UsagePeriod, number>> = { "7d": 7, "30d": 30, "90d": 90 };

// 최근 N일은 오늘을 포함한 N일 전체(로컬 자정 기준), 달은 그 달 1일부터 다음 달 1일 전까지다.
export function usageRange(period: UsagePeriod, now: Date): UsageRange {
  const year = now.getFullYear();
  const month = now.getMonth();
  const day = now.getDate();
  let from: Date;
  let to: Date;
  const days = RECENT_DAYS[period];
  if (days !== undefined) {
    from = new Date(year, month, day - (days - 1));
    to = new Date(year, month, day + 1);
  } else if (period === "this_month") {
    from = new Date(year, month, 1);
    to = new Date(year, month + 1, 1);
  } else {
    from = new Date(year, month - 1, 1);
    to = new Date(year, month, 1);
  }
  return {
    from: from.toISOString(),
    to: to.toISOString(),
    tzOffsetMinutes: -now.getTimezoneOffset(),
  };
}

export interface UsageTableRow {
  key: string;
  label: string;
  deleted: boolean;
  runs: string;
  inputTokens: string;
  outputTokens: string;
  reasoningTokens: string;
  cachedInputTokens: string;
  // "$0.0012" 또는 "-"(비용이 기록된 런이 없다)
  cost: string;
  // 비용 없이 토큰만 남은 런이 섞여 있어 cost가 그 런을 빼고 더한 값이다.
  costPartial: boolean;
  estimated: boolean;
  // 이 행의 토큰(입력+출력)이 전체에서 차지하는 비율(0~1)
  share: number;
}

export interface UsageTable {
  rows: UsageTableRow[];
  total: UsageTableRow;
  hasCost: boolean;
  unpricedRuns: number;
  estimated: boolean;
}

type SettingsT = TFunction<"settings">;

const dayLabel = new Intl.DateTimeFormat("ko-KR", {
  month: "long",
  day: "numeric",
  weekday: "short",
  timeZone: "UTC",
});

export function formatCost(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `$${formatUsd(value)}`;
}

export function usageRowLabel(row: UsageRow, groupBy: UsageGroupBy, t: SettingsT): string {
  if (groupBy === "day") {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(row.key ?? "");
    if (!match) return row.key ?? "-";
    return dayLabel.format(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  }
  // 에이전트로 묶으면 워크플로우 런이, 워크플로우로 묶으면 에이전트 런(자식 포함)이 키 없는 한 행에 모인다
  // (2단계 3.10). 어떻게 묶든 합계가 같다.
  if (groupBy === "agent") {
    if (row.key === null || row.key === undefined) return t("usage.workflowRuns");
    const name = row.label ?? t("usage.unknownAgent");
    return row.deleted ? t("usage.deletedAgent", { name }) : name;
  }
  if (groupBy === "workflow") {
    if (row.key === null || row.key === undefined) return t("usage.agentRuns");
    const name = row.label ?? t("usage.unknownWorkflow");
    return row.deleted ? t("usage.deletedWorkflow", { name }) : name;
  }
  return row.key ?? t("usage.unknownModel");
}

function tokens(row: UsageTotals): number {
  return (row.input_tokens ?? 0) + (row.output_tokens ?? 0);
}

function tableRow(
  key: string,
  label: string,
  row: UsageTotals,
  totalTokens: number,
  deleted = false,
): UsageTableRow {
  const unpriced = row.unpriced_runs ?? 0;
  return {
    key,
    label,
    deleted,
    runs: formatNumber(row.runs ?? 0),
    inputTokens: formatNumber(row.input_tokens ?? 0),
    outputTokens: formatNumber(row.output_tokens ?? 0),
    reasoningTokens: formatNumber(row.reasoning_tokens ?? 0),
    cachedInputTokens: formatNumber(row.cached_input_tokens ?? 0),
    cost: formatCost(row.cost_usd),
    costPartial: row.cost_usd !== null && row.cost_usd !== undefined && unpriced > 0,
    estimated: row.estimated ?? false,
    share: totalTokens > 0 ? tokens(row) / totalTokens : 0,
  };
}

export function buildUsageTable(report: UsageReport, t: SettingsT): UsageTable {
  const totalTokens = tokens(report.total);
  const rows = report.items.map((row, index) =>
    tableRow(
      row.key ?? `unknown-${index}`,
      usageRowLabel(row, report.group_by, t),
      row,
      totalTokens,
      row.deleted ?? false,
    ),
  );
  return {
    rows,
    total: tableRow("total", t("usage.total"), report.total, totalTokens),
    hasCost: report.total.cost_usd !== null && report.total.cost_usd !== undefined,
    unpricedRuns: report.total.unpriced_runs ?? 0,
    estimated: report.total.estimated ?? false,
  };
}
