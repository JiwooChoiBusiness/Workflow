import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { ApiError, apiFetch, isApiError, requestJson, uploadFile, withQuery } from "@/api/http";
import type { WfFileOut, WfMessageCreate, WfRunOut } from "@/api/types";
import type { ChatflowApi } from "@/features/workflows/chatflowSession";
import type { WfRunApi } from "@/features/workflows/wfRunController";

// 웹앱의 워크플로우·챗플로우(설계 6.2, 7.4)는 /api/web/flows, /api/web/wf-runs, /api/web/wf-conversations만
// 부른다. 로그인은 콘솔과 같은 쿠키다.

const enc = encodeURIComponent;
const RUNS_PAGE = 20;
const CONVERSATIONS_PAGE = 30;

export const flowKeys = {
  app: (slug: string) => ["webapp-flow", slug] as const,
  runs: (slug: string) => ["webapp-flow", slug, "runs"] as const,
  conversations: (slug: string) => ["webapp-flow", slug, "conversations"] as const,
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

export function flowConversationFileUrl(conversationId: string, fileId: string): string {
  return `/api/web/wf-conversations/${enc(conversationId)}/files/${enc(fileId)}/download`;
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

// 챗플로우 웹앱의 대화 목록(최근 대화 순). 미리보기·서비스 API 대화는 섞이지 않는다.
export function useFlowConversations(slug: string) {
  return useInfiniteQuery({
    queryKey: flowKeys.conversations(slug),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/web/flows/{slug}/conversations", {
          params: { path: { slug }, query: { limit: CONVERSATIONS_PAGE, offset: pageParam } },
        }),
      ),
    getNextPageParam: (page) => page.next_offset ?? undefined,
  });
}

export async function createFlowConversation(slug: string): Promise<string> {
  const created = await unwrap(
    api.POST("/api/web/flows/{slug}/conversations", { params: { path: { slug } }, body: {} }),
  );
  return created.id;
}

const conversationPath = (id: string) => ({ params: { path: { conversation_id: id } } });

export function renameFlowConversation(conversationId: string, title: string) {
  return unwrap(
    api.PATCH("/api/web/wf-conversations/{conversation_id}", {
      ...conversationPath(conversationId),
      body: { title },
    }),
  );
}

export function deleteFlowConversation(conversationId: string) {
  return unwrap(
    api.DELETE("/api/web/wf-conversations/{conversation_id}", conversationPath(conversationId)),
  );
}

/*
 * 채팅이 쓰는 서버 호출의 웹앱 판. 웹앱 API는 대화가 어느 챗플로우의 것인지 묻지 않으므로 주소에 다른
 * 웹앱의 대화를 넣으면 없는 대화(404)로 다룬다.
 */
export function webChatApi(slug: string, workflowId: string): ChatflowApi {
  return {
    getConversation: async (conversationId) => {
      const detail = await unwrap(
        api.GET("/api/web/wf-conversations/{conversation_id}", conversationPath(conversationId)),
      );
      if (detail.workflow_id !== workflowId) {
        throw new ApiError(404, "conversation_not_found", "이 웹앱의 대화가 아니다");
      }
      return detail;
    },
    listMessages: (conversationId) =>
      unwrap(
        api.GET("/api/web/wf-conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId }, query: { limit: 100 } },
        }),
      ),
    messageRequest: (conversationId: string, body: WfMessageCreate) =>
      new Request(`/api/web/wf-conversations/${enc(conversationId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
      }),
    upload: (file) => uploadFlowFile(slug, file),
    stop: (runId) => webWfRunApi.stop(runId),
    fileUrl: flowConversationFileUrl,
  };
}
