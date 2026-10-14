import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { apiFetch, networkError, toApiError } from "@/api/http";
import { agentKeys } from "@/features/agents/api";
import { runListQuery, sessionListQuery, type LogFilters } from "./filters";
import { isRunActive, type RunEventOut, type RunLogItem, type RunOut } from "./types";

const PAGE_SIZE = 50;
const EVENTS_PAGE = 1000;
// 이벤트가 이보다 많은 런은 앞부분만 보인다. 원본은 GET /runs/{rid}/events로 이어서 받는다.
const MAX_EVENT_PAGES = 20;
const ACTIVE_REFRESH_MS = 3000;
// 이보다 큰 도구 출력은 화면에 펼치지 않고 내려받게 한다.
export const TOOL_OUTPUT_VIEW_LIMIT = 1024 * 1024;

// 채팅 화면(features/sessions)과 같은 서버 자원을 읽지만 응답을 다르게 쓰므로 캐시 키를 나눈다.
export const logKeys = {
  all: ["logs"] as const,
  sessions: (filters: LogFilters) => ["logs", "sessions", filters] as const,
  runs: (filters: LogFilters) => ["logs", "runs", filters] as const,
  session: (sid: string) => ["logs", "session", sid] as const,
  messages: (sid: string) => ["logs", "session", sid, "messages"] as const,
  sessionRuns: (sid: string) => ["logs", "session", sid, "runs"] as const,
  files: (sid: string) => ["logs", "session", sid, "files"] as const,
  run: (rid: string) => ["logs", "run", rid] as const,
  children: (rid: string) => ["logs", "run", rid, "children"] as const,
  treeUsage: (rid: string) => ["logs", "run", rid, "tree-usage"] as const,
  events: (rid: string) => ["logs", "run", rid, "events"] as const,
  toolOutput: (rid: string, tcid: string) => ["logs", "run", rid, "tool-output", tcid] as const,
};

const sessionPath = (sid: string) => ({ params: { path: { session_id: sid } } });
const runPath = (rid: string) => ({ params: { path: { run_id: rid } } });

// 에이전트 화면과 같은 캐시 키를 쓴다. 지운 에이전트는 404라 오류 상태가 된다.
export function useAgentOrNull(agentId: string | null) {
  return useQuery({
    queryKey: agentKeys.detail(agentId ?? ""),
    enabled: agentId !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/agents/{agent_id}", { params: { path: { agent_id: agentId! } } }),
      ),
  });
}

export function useLogSessions(filters: LogFilters) {
  return useInfiniteQuery({
    queryKey: logKeys.sessions(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/console/sessions", {
          params: {
            query: {
              ...sessionListQuery(filters, Date.now()),
              limit: PAGE_SIZE,
              offset: pageParam,
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_offset ?? undefined,
  });
}

export function useLogRuns(filters: LogFilters) {
  return useInfiniteQuery({
    queryKey: logKeys.runs(filters),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/console/runs", {
          params: {
            query: { ...runListQuery(filters, Date.now()), limit: PAGE_SIZE, offset: pageParam },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_offset ?? undefined,
  });
}

export function useLogSession(sid: string) {
  return useQuery({
    queryKey: logKeys.session(sid),
    queryFn: () => unwrap(api.GET("/api/console/sessions/{session_id}", sessionPath(sid))),
    refetchInterval: (query) => (query.state.data?.active_run ? ACTIVE_REFRESH_MS : false),
  });
}

// 메시지는 끝에서부터 100개씩 읽는다. 앞 페이지는 "이전 메시지 더 보기"로 받는다.
export function useSessionMessages(sid: string, { enabled = true } = {}) {
  return useInfiniteQuery({
    queryKey: logKeys.messages(sid),
    enabled,
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/console/sessions/{session_id}/messages", {
          params: {
            path: { session_id: sid },
            query: pageParam === null ? { limit: 100 } : { limit: 100, before_seq: pageParam },
          },
        }),
      ),
    getNextPageParam: (last) => (last.has_more && last.items[0] ? last.items[0].seq : undefined),
  });
}

export function useSessionRuns(sid: string) {
  return useQuery({
    queryKey: logKeys.sessionRuns(sid),
    queryFn: async (): Promise<RunLogItem[]> =>
      (
        await unwrap(
          api.GET("/api/console/runs", { params: { query: { session_id: sid, limit: 200 } } }),
        )
      ).items,
    refetchInterval: (query) =>
      query.state.data?.some((run) => isRunActive(run.status)) ? ACTIVE_REFRESH_MS : false,
  });
}

export function useSessionFiles(sid: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: logKeys.files(sid),
    enabled,
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/sessions/{session_id}/files", sessionPath(sid)))).items,
  });
}

export function useRun(rid: string | null) {
  return useQuery({
    queryKey: logKeys.run(rid ?? ""),
    enabled: rid !== null,
    queryFn: (): Promise<RunOut> => unwrap(api.GET("/api/console/runs/{run_id}", runPath(rid!))),
    refetchInterval: (query) => (isRunActive(query.state.data?.status) ? ACTIVE_REFRESH_MS : false),
  });
}

// 깊이 한도(ORCHESTRATION_MAX_DEPTH)가 3까지라 한 런 아래로 세 단계면 트리 전체다.
const MAX_CHILD_DEPTH = 3;
const CHILD_PAGE = 200;

async function fetchChildRuns(rid: string): Promise<RunLogItem[]> {
  const body = await unwrap(
    api.GET("/api/console/runs", { params: { query: { parent_run_id: rid, limit: CHILD_PAGE } } }),
  );
  // 목록은 최근 것부터다. 하위 런 탭은 부른 순서로 보여 준다.
  return [...body.items].sort((a, b) => a.queued_at.localeCompare(b.queued_at));
}

// call_agent로 부른 자식 런(오케스트레이션 6.2). 부모가 아직 열려 있으면 몇 초마다 다시 읽는다.
export function useChildRuns(rid: string, { live = false } = {}) {
  return useQuery({
    queryKey: logKeys.children(rid),
    queryFn: () => fetchChildRuns(rid),
    refetchInterval: (query) =>
      live || query.state.data?.some((run) => isRunActive(run.status)) ? ACTIVE_REFRESH_MS : false,
  });
}

export interface TreeUsage {
  inputTokens: number;
  outputTokens: number;
  // 가격표가 없는 런이 섞이면 null(알 수 없음)
  costUsd: number | null;
  estimated: boolean;
  runs: number;
}

// 자식·손자 런의 사용량 합. 부모는 자식 사용량을 더하지 않으므로(3.9) 화면이 parent_run_id로 모아 더한다.
async function fetchTreeUsage(rid: string): Promise<TreeUsage> {
  const total: TreeUsage = {
    inputTokens: 0,
    outputTokens: 0,
    costUsd: 0,
    estimated: false,
    runs: 0,
  };
  let level = await fetchChildRuns(rid);
  for (let depth = 0; depth < MAX_CHILD_DEPTH && level.length > 0; depth += 1) {
    for (const run of level) {
      total.inputTokens += run.usage.input_tokens ?? 0;
      total.outputTokens += run.usage.output_tokens ?? 0;
      const cost = run.usage.cost_usd;
      total.costUsd =
        cost === null || cost === undefined || total.costUsd === null ? null : total.costUsd + cost;
      total.estimated ||= Boolean(run.usage.estimated);
      total.runs += 1;
    }
    const parents = level.filter((run) => run.child_count > 0);
    level = (await Promise.all(parents.map((run) => fetchChildRuns(run.id)))).flat();
  }
  return total;
}

export function useTreeUsage(rid: string, { enabled = true, live = false } = {}) {
  return useQuery({
    queryKey: logKeys.treeUsage(rid),
    enabled,
    queryFn: () => fetchTreeUsage(rid),
    refetchInterval: live ? ACTIVE_REFRESH_MS : false,
  });
}

async function fetchAllEvents(rid: string): Promise<{ items: RunEventOut[]; truncated: boolean }> {
  const items: RunEventOut[] = [];
  let after = 0;
  for (let page = 0; page < MAX_EVENT_PAGES; page += 1) {
    const body = await unwrap(
      api.GET("/api/console/runs/{run_id}/events", {
        params: { path: { run_id: rid }, query: { after, limit: EVENTS_PAGE } },
      }),
    );
    items.push(...body.items);
    if (body.next_after === null || body.next_after === undefined) {
      return { items, truncated: false };
    }
    after = body.next_after;
  }
  return { items, truncated: true };
}

export function useRunEvents(rid: string | null, { live = false } = {}) {
  return useQuery({
    queryKey: logKeys.events(rid ?? ""),
    enabled: rid !== null,
    queryFn: () => fetchAllEvents(rid!),
    refetchInterval: live ? ACTIVE_REFRESH_MS : false,
  });
}

export function toolOutputUrl(runId: string, toolCallId: string): string {
  return `/api/console/runs/${encodeURIComponent(runId)}/tool-calls/${encodeURIComponent(toolCallId)}/output`;
}

export function sessionFileUrl(sessionId: string, fileId: string): string {
  return `/api/console/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}/download`;
}

async function fetchText(url: string): Promise<string> {
  let response: Response;
  try {
    response = await apiFetch(url);
  } catch (cause) {
    throw networkError(cause);
  }
  if (!response.ok) {
    // 오류 응답은 {error: {code, message}} JSON이다. JSON이 아니면 상태 코드로만 알린다.
    const body: unknown = await response.json().catch(() => undefined);
    throw toApiError(response, body);
  }
  return response.text();
}

// 도구 출력 전문(256KB를 넘으면 storage의 파일, 아니면 이벤트에 남은 전문).
export function useToolOutput(runId: string, toolCallId: string, { enabled = true } = {}) {
  return useQuery({
    queryKey: logKeys.toolOutput(runId, toolCallId),
    enabled,
    queryFn: () => fetchText(toolOutputUrl(runId, toolCallId)),
    staleTime: Infinity,
  });
}
