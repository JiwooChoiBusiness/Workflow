import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { ApiError, apiFetch, isApiError, requestJson, uploadFile, withQuery } from "@/api/http";
import type { components } from "@/api/schema";
import { HISTORY_LIMIT } from "@/features/sessions/api";
import type { ChatSessionApi } from "@/features/sessions/chatSession";
import type { RunOut, SessionDetail, SessionFileOut } from "@/features/sessions/types";

// 웹앱 화면(설계 6.3, 7.2)은 /api/web만 부른다. 로그인은 콘솔과 같은 쿠키다.

export type WebAppOut = components["schemas"]["WebAppOut"];

const SESSIONS_PAGE = 30;

export const webappKeys = {
  app: (slug: string) => ["webapp", slug] as const,
  sessions: (slug: string) => ["webapp", slug, "sessions"] as const,
};

const enc = encodeURIComponent;
const sessionPath = (id: string) => ({ params: { path: { session_id: id } } });
const runPath = (id: string) => ({ params: { path: { run_id: id } } });

export function useWebApp(slug: string) {
  return useQuery({
    queryKey: webappKeys.app(slug),
    queryFn: () => unwrap(api.GET("/api/web/apps/{slug}", { params: { path: { slug } } })),
    // 없는 웹앱(발행 안 함, 공개 안 함)은 다시 물어도 같다.
    retry: (count, error) => !isApiError(error, 404) && count < 2,
  });
}

export function useWebAppSessions(slug: string) {
  return useInfiniteQuery({
    queryKey: webappKeys.sessions(slug),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/web/apps/{slug}/sessions", {
          params: { path: { slug }, query: { limit: SESSIONS_PAGE, offset: pageParam } },
        }),
      ),
    getNextPageParam: (page) => page.next_offset ?? undefined,
  });
}

export async function createWebAppSession(slug: string): Promise<string> {
  const session = await unwrap(
    api.POST("/api/web/apps/{slug}/sessions", { params: { path: { slug } }, body: {} }),
  );
  return session.id;
}

export function webSessionFileUrl(sessionId: string, fileId: string): string {
  return `/api/web/sessions/${enc(sessionId)}/files/${enc(fileId)}/download`;
}

/*
 * 채팅 패널이 쓰는 서버 호출의 웹앱 판. 웹앱 API는 세션이 어느 앱의 것인지 묻지 않으므로, 주소의 다른
 * 앱 세션을 열면 없는 세션(404)으로 다룬다. onDetail은 세션 상세를 읽을 때마다 불린다(버전 표시용).
 * 하위 에이전트의 세션은 다른 에이전트의 것이므로 agentId 없이(null) 만든 것으로 연다. 서버가 부모 사슬로
 * 범위를 판단한다(오케스트레이션 6.3).
 */
export function webSessionApi(
  agentId: string | null,
  onDetail?: (detail: SessionDetail) => void,
): ChatSessionApi {
  return {
    getSession: async (sessionId) => {
      const detail = await unwrap(
        api.GET("/api/web/sessions/{session_id}", sessionPath(sessionId)),
      );
      if (agentId !== null && detail.agent_id !== agentId) {
        throw new ApiError(404, "session_not_found", "이 웹앱의 대화가 아니다");
      }
      onDetail?.(detail);
      return detail;
    },
    listMessages: (sessionId) =>
      unwrap(
        api.GET("/api/web/sessions/{session_id}/messages", {
          params: { path: { session_id: sessionId }, query: { limit: HISTORY_LIMIT } },
        }),
      ),
    upload: (sessionId, file) =>
      uploadFile<SessionFileOut>(`/api/web/sessions/${enc(sessionId)}/uploads`, file),
    stop: (runId) => unwrap(api.POST("/api/web/runs/{run_id}/stop", runPath(runId))),
    input: (runId, body) =>
      requestJson<RunOut>("POST", `/api/web/runs/${enc(runId)}/input`, {
        json: { ...body, response_mode: "none" },
      }),
    patchSession: (sessionId, body) =>
      unwrap(api.PATCH("/api/web/sessions/{session_id}", { ...sessionPath(sessionId), body })),
    messageRequest: (sessionId, body) =>
      new Request(`/api/web/sessions/${enc(sessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ ...body, response_mode: "streaming" }),
      }),
    eventsRequest: (sessionId, after, runId) =>
      new Request(
        withQuery(`/api/web/sessions/${enc(sessionId)}/events`, { after, run_id: runId }),
        { headers: { Accept: "text/event-stream" } },
      ),
    fetch: (request) => apiFetch(request),
  };
}
