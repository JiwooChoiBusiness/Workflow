import { RUN_STATUSES, SESSION_SOURCES, type RunStatus, type SessionSource } from "./types";

// 로그 목록의 필터(설계 7.2: 에이전트·소스·기간·상태). 주소의 검색 매개변수에 두어 새로 고침과 뒤로
// 가기에도 남고 링크로 그대로 옮길 수 있다.
export type LogTab = "sessions" | "runs";
export const LOG_PERIODS = ["all", "1d", "7d", "30d", "90d"] as const;
export type LogPeriod = (typeof LOG_PERIODS)[number];

export interface LogFilters {
  tab: LogTab;
  agent: string | null;
  source: SessionSource | null;
  period: LogPeriod;
  // 런 상태. 세션 목록에는 쓰지 않는다.
  status: RunStatus | null;
}

export const DEFAULT_LOG_FILTERS: LogFilters = {
  tab: "sessions",
  agent: null,
  source: null,
  period: "all",
  status: null,
};

const PERIOD_DAYS: Record<Exclude<LogPeriod, "all">, number> = {
  "1d": 1,
  "7d": 7,
  "30d": 30,
  "90d": 90,
};
const DAY_MS = 86_400_000;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

export function parseLogFilters(params: URLSearchParams): LogFilters {
  const agent = params.get("agent")?.trim();
  return {
    tab: params.get("tab") === "runs" ? "runs" : "sessions",
    agent: agent ? agent : null,
    source: oneOf(params.get("source"), SESSION_SOURCES),
    period: oneOf(params.get("period"), LOG_PERIODS) ?? DEFAULT_LOG_FILTERS.period,
    status: oneOf(params.get("status"), RUN_STATUSES),
  };
}

// 기본값은 주소에 남기지 않는다.
export function logFiltersToParams(filters: LogFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.tab !== DEFAULT_LOG_FILTERS.tab) params.set("tab", filters.tab);
  if (filters.agent) params.set("agent", filters.agent);
  if (filters.source) params.set("source", filters.source);
  if (filters.period !== DEFAULT_LOG_FILTERS.period) params.set("period", filters.period);
  if (filters.status) params.set("status", filters.status);
  return params;
}

export function hasActiveFilters(filters: LogFilters, { ignoreAgent = false } = {}): boolean {
  return (
    (!ignoreAgent && filters.agent !== null) ||
    filters.source !== null ||
    filters.period !== DEFAULT_LOG_FILTERS.period ||
    (filters.tab === "runs" && filters.status !== null)
  );
}

export function clearFilters(filters: LogFilters, { keepAgent = false } = {}): LogFilters {
  return { ...DEFAULT_LOG_FILTERS, tab: filters.tab, agent: keepAgent ? filters.agent : null };
}

export function periodSince(period: LogPeriod, now: number): string | undefined {
  if (period === "all") return undefined;
  return new Date(now - PERIOD_DAYS[period] * DAY_MS).toISOString();
}

export interface SessionListQuery {
  agent_id?: string;
  source?: SessionSource;
  since?: string;
}

export interface RunListQuery extends SessionListQuery {
  status?: RunStatus;
}

export function sessionListQuery(filters: LogFilters, now: number): SessionListQuery {
  const query: SessionListQuery = {};
  if (filters.agent) query.agent_id = filters.agent;
  if (filters.source) query.source = filters.source;
  const since = periodSince(filters.period, now);
  if (since) query.since = since;
  return query;
}

export function runListQuery(filters: LogFilters, now: number): RunListQuery {
  const query: RunListQuery = sessionListQuery(filters, now);
  if (filters.status) query.status = filters.status;
  return query;
}

// 세션 상세 주소. 런을 주면 그 런을 인스펙터에 연다.
export function sessionLogPath(sessionId: string, runId?: string): string {
  const base = `/logs/sessions/${encodeURIComponent(sessionId)}`;
  return runId ? `${base}?run=${encodeURIComponent(runId)}` : base;
}
