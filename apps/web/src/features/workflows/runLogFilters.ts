import type { WfRunOut } from "@/api/types";
import { LOG_PERIODS, periodSince, type LogPeriod } from "@/features/logs/filters";
import { RUN_STATUSES, type RunStatus } from "@/features/logs/types";
import type { RunListQuery } from "./api";

// 워크플로우 런 목록의 필터(/workflows/:id/logs, /logs의 워크플로우 런 탭). 주소의 검색 매개변수에 둔다.
export type WfTrigger = WfRunOut["trigger"];
// resume은 런을 만들 때 쓰지 않는다(재개해도 처음 trigger가 남는다).
export const WF_TRIGGERS: readonly WfTrigger[] = ["preview", "webapp", "api", "single_node"];

export interface WfRunFilters {
  workflow: string | null;
  trigger: WfTrigger | null;
  status: RunStatus | null;
  period: LogPeriod;
}

export const DEFAULT_WF_RUN_FILTERS: WfRunFilters = {
  workflow: null,
  trigger: null,
  status: null,
  period: "all",
};

const KEYS = ["workflow", "trigger", "status", "period"] as const;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

export function parseWfRunFilters(params: URLSearchParams): WfRunFilters {
  const workflow = params.get("workflow")?.trim();
  return {
    workflow: workflow ? workflow : null,
    trigger: oneOf(params.get("trigger"), WF_TRIGGERS),
    status: oneOf(params.get("status"), RUN_STATUSES),
    period: oneOf(params.get("period"), LOG_PERIODS) ?? DEFAULT_WF_RUN_FILTERS.period,
  };
}

// base의 다른 매개변수(탭 등)는 두고 필터 값만 바꾼다. 기본값은 남기지 않는다.
export function wfRunFiltersToParams(
  filters: WfRunFilters,
  base: URLSearchParams = new URLSearchParams(),
): URLSearchParams {
  const params = new URLSearchParams(base);
  for (const key of KEYS) params.delete(key);
  if (filters.workflow) params.set("workflow", filters.workflow);
  if (filters.trigger) params.set("trigger", filters.trigger);
  if (filters.status) params.set("status", filters.status);
  if (filters.period !== DEFAULT_WF_RUN_FILTERS.period) params.set("period", filters.period);
  return params;
}

export function hasWfRunFilters(filters: WfRunFilters, { ignoreWorkflow = false } = {}): boolean {
  return (
    (!ignoreWorkflow && filters.workflow !== null) ||
    filters.trigger !== null ||
    filters.status !== null ||
    filters.period !== DEFAULT_WF_RUN_FILTERS.period
  );
}

export function wfRunListQuery(
  filters: WfRunFilters,
  now: number,
): Omit<RunListQuery, "limit" | "offset"> {
  const query: Omit<RunListQuery, "limit" | "offset"> = {};
  if (filters.trigger) query.trigger = filters.trigger;
  if (filters.status) query.status = filters.status;
  const since = periodSince(filters.period, now);
  if (since) query.since = since;
  return query;
}

export function wfRunPath(workflowId: string, runId: string): string {
  return `/workflows/${encodeURIComponent(workflowId)}/logs/${encodeURIComponent(runId)}`;
}

// 런 id(uuid v7)는 앞 12자리가 만든 시각이라 같은 분에 만든 런끼리 겹친다. 짧게 보일 때는 끝을 쓴다.
export function shortRunId(runId: string): string {
  return runId.slice(-8);
}

export function wfRunDuration(run: Pick<WfRunOut, "started_at" | "ended_at">): number | null {
  if (!run.started_at || !run.ended_at) return null;
  return new Date(run.ended_at).getTime() - new Date(run.started_at).getTime();
}
