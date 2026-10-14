import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { apiFetch, isApiError, requestJson, uploadFile, withQuery } from "@/api/http";
import type { WfFileOut, WfRunOut } from "@/api/types";
import type { WfRunApi } from "@/features/workflows/wfRunController";

// 웹앱의 워크플로우(설계 6.2, 7.4)는 /api/web/flows와 /api/web/wf-runs만 부른다. 로그인은 콘솔과 같은 쿠키다.

const enc = encodeURIComponent;
const RUNS_PAGE = 20;

export const flowKeys = {
  app: (slug: string) => ["webapp-flow", slug] as const,
  runs: (slug: string) => ["webapp-flow", slug, "runs"] as const,
};

export function useFlowApp(slug: string) {
  return useQuery({
    queryKey: flowKeys.app(slug),
    queryFn: () => unwrap(api.GET("/api/web/flows/{slug}", { params: { path: { slug } } })),
    // 없는 웹앱(발행 안 함, 공개 안 함)은 다시 물어도 같다.
    retry: (count, error) => !isApiError(error, 404) && count < 2,
  });
}

// 이 웹앱에서 실행한 런(최근 것부터). 진행 중인 런이 있으면 몇 초마다 다시 읽는다.
export function useFlowRuns(slug: string) {
  return useInfiniteQuery({
    queryKey: flowKeys.runs(slug),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/web/flows/{slug}/runs", {
          params: { path: { slug }, query: { limit: RUNS_PAGE, offset: pageParam } },
        }),
      ),
    getNextPageParam: (page) => page.next_offset ?? undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.items.some((run) => run.status === "queued" || run.status === "running"),
      )
        ? 3000
        : false,
  });
}

// 끝난 런의 전문 출력. 이벤트의 출력은 4KB 미리보기라 잘렸을 때만 다시 받는다.
export function useFlowRunDetail(runId: string | null) {
  return useQuery({
    queryKey: ["webapp-flow-run", runId ?? ""] as const,
    enabled: runId !== null,
    queryFn: () =>
      unwrap(api.GET("/api/web/wf-runs/{run_id}", { params: { path: { run_id: runId ?? "" } } })),
    staleTime: Infinity,
  });
}

export function flowRunRequest(slug: string, inputs: Record<string, unknown>): Request {
  return new Request(`/api/web/flows/${enc(slug)}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ inputs, response_mode: "streaming" }),
  });
}

export function uploadFlowFile(slug: string, file: File): Promise<WfFileOut> {
  return uploadFile<WfFileOut>(`/api/web/flows/${enc(slug)}/files`, file);
}

export function flowFileUrl(runId: string, fileId: string): string {
  return `/api/web/wf-runs/${enc(runId)}/files/${enc(fileId)}/download`;
}

export const webWfRunApi: WfRunApi = {
  eventsRequest: (runId, after) =>
    new Request(withQuery(`/api/web/wf-runs/${enc(runId)}/events`, { after }), {
      headers: { Accept: "text/event-stream" },
    }),
  stop: (runId) =>
    unwrap(api.POST("/api/web/wf-runs/{run_id}/stop", { params: { path: { run_id: runId } } })),
  input: (runId, body) =>
    requestJson<WfRunOut>("POST", `/api/web/wf-runs/${enc(runId)}/input`, {
      json: { ...body, response_mode: "none" },
    }),
  fetch: (request) => apiFetch(request),
};
