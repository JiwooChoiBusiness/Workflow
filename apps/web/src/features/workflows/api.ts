/*
 * 워크플로우 콘솔 API(설계 6.1). 편집기(M15)가 쓰는 목록·초안·검증·발행·버전·노드 타입과, 실행
 * 패널(M16)이 쓸 미리보기 런·단일 노드 실행·런 조회·SSE·입력·중지·파일 업로드를 함께 둔다.
 */
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { requestJson, uploadFile, withQuery } from "@/api/http";
import type {
  ApiKeyCreate,
  ApiKeyCreated,
  ApiKeyOut,
  WfFileOut,
  WfNodeRunCreate,
  WfRunCreate,
  WfRunOut,
  WorkflowCreate,
  WorkflowDraftCheck,
  WorkflowDraftUpdate,
  WorkflowKind,
  WorkflowOut,
  WorkflowPatch,
  WorkflowVersionOut,
} from "@/api/types";
import type { RunInputBody } from "@/features/sessions/types";
import { wfRunListQuery, type WfRunFilters } from "./runLogFilters";

export const workflowKeys = {
  all: ["workflows"] as const,
  list: (kind?: WorkflowKind) => ["workflows", "list", kind ?? "all"] as const,
  detail: (id: string) => ["workflows", id] as const,
  draft: (id: string) => ["workflows", id, "draft"] as const,
  versions: (id: string) => ["workflows", id, "versions"] as const,
  version: (id: string, versionId: string) => ["workflows", id, "versions", versionId] as const,
  apiKeys: (id: string) => ["workflows", id, "api-keys"] as const,
  nodeTypes: ["workflows", "node-types"] as const,
  run: (runId: string) => ["wf-runs", runId] as const,
  nodeRun: (runId: string, nodeRunId: string) =>
    ["wf-runs", runId, "node-runs", nodeRunId] as const,
  eventLog: (runId: string) => ["wf-runs", runId, "event-log"] as const,
};

const workflowPath = (id: string) => ({ params: { path: { workflow_id: id } } });
const runPath = (runId: string) => ({ params: { path: { run_id: runId } } });

export function useWorkflows() {
  return useQuery({
    queryKey: workflowKeys.list(),
    queryFn: async () => (await unwrap(api.GET("/api/console/workflows"))).items,
  });
}

export function useWorkflow(id: string) {
  return useQuery({
    queryKey: workflowKeys.detail(id),
    queryFn: () => unwrap(api.GET("/api/console/workflows/{workflow_id}", workflowPath(id))),
  });
}

function refreshList(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: [...workflowKeys.all, "list"] });
}

export function useCreateWorkflow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkflowCreate) => unwrap(api.POST("/api/console/workflows", { body })),
    onSuccess: (workflow) => {
      client.setQueryData(workflowKeys.detail(workflow.id), workflow);
      refreshList(client);
    },
  });
}

export function usePatchWorkflow(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkflowPatch) =>
      unwrap(api.PATCH("/api/console/workflows/{workflow_id}", { ...workflowPath(id), body })),
    onSuccess: (workflow) => {
      client.setQueryData(workflowKeys.detail(id), workflow);
      refreshList(client);
    },
  });
}

export function useDeleteWorkflow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE("/api/console/workflows/{workflow_id}", workflowPath(id))),
    onSuccess: (_data, id) => {
      client.removeQueries({ queryKey: workflowKeys.detail(id) });
      refreshList(client);
    },
  });
}

export function fetchDraft(id: string): Promise<WorkflowVersionOut> {
  return unwrap(api.GET("/api/console/workflows/{workflow_id}/draft", workflowPath(id)));
}

export function useWorkflowDraft(id: string) {
  return useQuery({
    queryKey: workflowKeys.draft(id),
    queryFn: () => fetchDraft(id),
    // 편집 중에 다시 읽어 캔버스를 덮지 않는다. 충돌 때만 직접 다시 불러온다.
    staleTime: Infinity,
    gcTime: 0,
  });
}

// 자동 저장이 부른다. 409 draft_conflict는 다른 탭(곳)에서 먼저 저장한 것이다.
export function saveDraft(id: string, body: WorkflowDraftUpdate): Promise<WorkflowVersionOut> {
  return unwrap(
    api.PUT("/api/console/workflows/{workflow_id}/draft", { ...workflowPath(id), body }),
  );
}

export function validateDraft(id: string, body: WorkflowDraftCheck) {
  return unwrap(
    api.POST("/api/console/workflows/{workflow_id}/draft/validate", { ...workflowPath(id), body }),
  );
}

export function useNodeTypes() {
  return useQuery({
    queryKey: workflowKeys.nodeTypes,
    queryFn: async () => (await unwrap(api.GET("/api/console/node-types"))).items,
    // 서버를 다시 띄우기 전에는 바뀌지 않는다.
    staleTime: Infinity,
  });
}

export function useWorkflowVersions(id: string) {
  return useQuery({
    queryKey: workflowKeys.versions(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/workflows/{workflow_id}/versions", workflowPath(id))))
        .items,
  });
}

export function useWorkflowVersion(id: string, versionId: string | null) {
  return useQuery({
    queryKey: workflowKeys.version(id, versionId ?? ""),
    enabled: versionId !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/workflows/{workflow_id}/versions/{version_id}", {
          params: { path: { workflow_id: id, version_id: versionId ?? "" } },
        }),
      ),
    staleTime: Infinity,
  });
}

export function usePublishWorkflow(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (changeNote: string | null) =>
      unwrap(
        api.POST("/api/console/workflows/{workflow_id}/publish", {
          ...workflowPath(id),
          body: { change_note: changeNote },
        }),
      ),
    onSuccess: (published) => {
      client.setQueryData<WorkflowOut>(workflowKeys.detail(id), published.workflow);
      void client.invalidateQueries({ queryKey: workflowKeys.versions(id) });
      refreshList(client);
    },
  });
}

export function useRestoreWorkflowVersion(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (versionId: string) =>
      unwrap(
        api.POST("/api/console/workflows/{workflow_id}/versions/{version_id}/restore", {
          params: { path: { workflow_id: id, version_id: versionId } },
        }),
      ),
    onSuccess: (workflow) => {
      client.setQueryData<WorkflowOut>(workflowKeys.detail(id), workflow);
      void client.invalidateQueries({ queryKey: workflowKeys.versions(id) });
      refreshList(client);
    },
  });
}

// 워크플로우용 API 키는 1단계 ApiKeyService를 대상만 바꿔 쓰는 경로다(설계 6.1). 서버 쪽 키 대상
// 변경(api_key.workflow_id)이 들어오기 전에는 404이고 화면은 "서버 API가 아직 없다"로 보인다.
const apiKeysPath = (id: string) => `/api/console/workflows/${encodeURIComponent(id)}/api-keys`;

export function useWorkflowApiKeys(id: string) {
  return useQuery({
    queryKey: workflowKeys.apiKeys(id),
    queryFn: async () => (await requestJson<{ items: ApiKeyOut[] }>("GET", apiKeysPath(id))).items,
  });
}

export function useCreateWorkflowApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ApiKeyCreate) =>
      requestJson<ApiKeyCreated>("POST", apiKeysPath(id), { json: body }),
    onSuccess: () => client.invalidateQueries({ queryKey: workflowKeys.apiKeys(id) }),
  });
}

export function useRevokeWorkflowApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (keyId: string) =>
      requestJson<void>("DELETE", `${apiKeysPath(id)}/${encodeURIComponent(keyId)}`),
    onSuccess: () => client.invalidateQueries({ queryKey: workflowKeys.apiKeys(id) }),
  });
}

// 미리보기 런의 파일 입력. 받은 id를 start의 file 필드 값으로 넣는다.
export function uploadWorkflowFile(id: string, file: File): Promise<WfFileOut> {
  return uploadFile<WfFileOut>(`/api/console/workflows/${encodeURIComponent(id)}/files`, file);
}

// streamRunEvents(src/lib/sse.ts)에 넘기는 요청. streaming이면 응답이 SSE이고 X-Run-Id 헤더가 붙는다.
export function draftRunRequest(id: string, body: WfRunCreate): Request {
  return new Request(`/api/console/workflows/${encodeURIComponent(id)}/draft/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// 단일 노드 실행(설계 6.1). inputs 키는 노드가 참조하는 "<scope>.<name>"이다.
export function nodeRunRequest(id: string, nodeId: string, body: WfNodeRunCreate): Request {
  return new Request(
    `/api/console/workflows/${encodeURIComponent(id)}/draft/nodes/${encodeURIComponent(nodeId)}/runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

export function wfRunEventsRequest(runId: string, after: number): Request {
  return new Request(
    withQuery(`/api/console/wf-runs/${encodeURIComponent(runId)}/events`, { after }),
  );
}

export interface RunListQuery {
  status?: WfRunOut["status"];
  trigger?: WfRunOut["trigger"];
  since?: string;
  limit?: number;
  offset?: number;
}

export function fetchWorkflowRuns(id: string, query: RunListQuery = {}) {
  return unwrap(
    api.GET("/api/console/workflows/{workflow_id}/runs", {
      params: { path: { workflow_id: id }, query },
    }),
  );
}

const RUNS_PAGE = 50;
// 진행 중인 런이 보이면 목록을 몇 초마다 다시 읽는다.
const ACTIVE_REFRESH_MS = 3000;

function hasActive(items: readonly WfRunOut[]): boolean {
  return items.some((run) => run.status === "queued" || run.status === "running");
}

// 워크플로우 하나의 실행 기록(설계 7.2 /workflows/:id/logs). 최근 것부터 50개씩. 기간은 읽을 때 정한다.
export function useWorkflowRunPages(id: string, filters: WfRunFilters) {
  return useInfiniteQuery({
    queryKey: [...workflowKeys.detail(id), "runs", "pages", filters] as const,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      fetchWorkflowRuns(id, {
        ...wfRunListQuery(filters, Date.now()),
        limit: RUNS_PAGE,
        offset: pageParam,
      }),
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: (state) =>
      state.state.data?.pages.some((page) => hasActive(page.items)) ? ACTIVE_REFRESH_MS : false,
  });
}

/*
 * 여러 워크플로우의 실행 기록을 시작 시각 순으로 합친다(/logs의 워크플로우 런 탭). 서버에 워크스페이스 전체
 * 런 목록 경로가 없어 워크플로우마다 앞에서 limit개를 받아 섞는다. 지운 워크플로우의 런은 받지 못한다.
 */
export function useMergedWorkflowRuns(
  workflowIds: readonly string[],
  filters: WfRunFilters,
  limit: number,
) {
  return useQuery({
    queryKey: ["wf-run-log", [...workflowIds].sort(), filters, limit] as const,
    enabled: workflowIds.length > 0,
    queryFn: async () => {
      const query = wfRunListQuery(filters, Date.now());
      const lists = await Promise.all(
        workflowIds.map((id) => fetchWorkflowRuns(id, { ...query, limit, offset: 0 })),
      );
      const items = lists
        .flatMap((list) => list.items)
        .sort((a, b) => b.queued_at.localeCompare(a.queued_at));
      return {
        items: items.slice(0, limit),
        more:
          items.length > limit ||
          lists.some((list) => list.next_offset !== null && list.next_offset !== undefined),
      };
    },
    placeholderData: keepPreviousData,
    refetchInterval: (state) =>
      state.state.data && hasActive(state.state.data.items) ? ACTIVE_REFRESH_MS : false,
  });
}

export function fetchWfRun(runId: string) {
  return unwrap(api.GET("/api/console/wf-runs/{run_id}", runPath(runId)));
}

// 런 상세. 도는 동안에는 몇 초마다 다시 읽는다.
export function useWfRun(runId: string | null) {
  return useQuery({
    queryKey: workflowKeys.run(runId ?? ""),
    enabled: runId !== null,
    queryFn: () => fetchWfRun(runId ?? ""),
    refetchInterval: (state) =>
      state.state.data?.status === "queued" || state.state.data?.status === "running"
        ? ACTIVE_REFRESH_MS
        : false,
  });
}

export async function fetchWorkflowNodeRuns(runId: string, iteration?: string) {
  const body = await unwrap(
    api.GET("/api/console/wf-runs/{run_id}/node-runs", {
      params: { path: { run_id: runId }, query: iteration ? { iteration } : {} },
    }),
  );
  return body.items;
}

// 노드 런 전문(inputs·outputs·process). 실행 패널이 카드를 펼칠 때만 부른다(설계 3.9).
export function useWfNodeRun(runId: string, nodeRunId: string | null) {
  return useQuery({
    queryKey: workflowKeys.nodeRun(runId, nodeRunId ?? ""),
    enabled: nodeRunId !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/wf-runs/{run_id}/node-runs/{node_run_id}", {
          params: { path: { run_id: runId, node_run_id: nodeRunId ?? "" } },
        }),
      ),
    staleTime: Infinity,
  });
}

// 영속 이벤트 페이지(로그 화면의 이벤트 표). 진행 중인 런은 wfRunEventsRequest(SSE)로 따라간다.
export function fetchWfRunEventLog(runId: string, after = 0, limit = 500) {
  return unwrap(
    api.GET("/api/console/wf-runs/{run_id}/event-log", {
      params: { path: { run_id: runId }, query: { after, limit } },
    }),
  );
}

const EVENTS_PAGE = 1000;
// 이벤트가 이보다 많은 런은 앞부분만 표에 보인다.
const MAX_EVENT_PAGES = 20;

export async function fetchAllWfRunEvents(runId: string) {
  const items: Awaited<ReturnType<typeof fetchWfRunEventLog>>["items"] = [];
  let after = 0;
  for (let page = 0; page < MAX_EVENT_PAGES; page += 1) {
    const body = await fetchWfRunEventLog(runId, after, EVENTS_PAGE);
    items.push(...body.items);
    if (body.next_after === null || body.next_after === undefined) {
      return { items, truncated: false };
    }
    after = body.next_after;
  }
  return { items, truncated: true };
}

export function useWfRunEventLog(runId: string, { live = false } = {}) {
  return useQuery({
    queryKey: workflowKeys.eventLog(runId),
    queryFn: () => fetchAllWfRunEvents(runId),
    refetchInterval: live ? ACTIVE_REFRESH_MS : false,
  });
}

export function stopWfRun(runId: string): Promise<WfRunOut> {
  return unwrap(api.POST("/api/console/wf-runs/{run_id}/stop", runPath(runId)));
}

// 승인·질문 응답. 202와 런 요약이 오고, 이어지는 이벤트는 events?after=로 받는다.
export function provideWfInput(runId: string, body: RunInputBody): Promise<WfRunOut> {
  return requestJson<WfRunOut>("POST", `/api/console/wf-runs/${encodeURIComponent(runId)}/input`, {
    json: { ...body, response_mode: "none" },
  });
}
