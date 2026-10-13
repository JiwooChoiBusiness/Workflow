import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { apiFetch, requestJson, uploadFile, withQuery } from "@/api/http";
import type {
  MessageList,
  RunInputBody,
  RunOut,
  SessionDetail,
  SessionFileOut,
  SessionOut,
  SessionPatch,
} from "./types";

export const sessionKeys = {
  all: ["sessions"] as const,
  detail: (id: string) => ["sessions", id] as const,
  messages: (id: string) => ["sessions", id, "messages"] as const,
  files: (id: string) => ["sessions", id, "files"] as const,
  workspace: (id: string) => ["sessions", id, "workspace"] as const,
};

// 채팅 패널이 한 번에 불러오는 메시지 수. 더 앞의 대화는 로그 화면에서 본다.
export const HISTORY_LIMIT = 200;

const enc = encodeURIComponent;
const sessionPath = (id: string) => ({ params: { path: { session_id: id } } });

export function fetchSessionDetail(sessionId: string): Promise<SessionDetail> {
  return unwrap(api.GET("/api/console/sessions/{session_id}", sessionPath(sessionId)));
}

export function fetchSessionMessages(
  sessionId: string,
  limit: number = HISTORY_LIMIT,
): Promise<MessageList> {
  return unwrap(
    api.GET("/api/console/sessions/{session_id}/messages", {
      params: { path: { session_id: sessionId }, query: { limit } },
    }),
  );
}

// 빌더 미리보기 세션. 에이전트의 초안을 쓴다(설계 6.2).
export function createPreviewSession(agentId: string): Promise<SessionOut> {
  return unwrap(
    api.POST("/api/console/agents/{agent_id}/preview/sessions", {
      params: { path: { agent_id: agentId } },
      body: {},
    }),
  );
}

// approval_override: "off"면 이 세션은 자동 승인, null이면 에이전트 버전의 승인 모드로 돌아간다(3.10).
export function patchSession(sessionId: string, body: SessionPatch): Promise<SessionOut> {
  return unwrap(
    api.PATCH("/api/console/sessions/{session_id}", { ...sessionPath(sessionId), body }),
  );
}

export function stopRun(runId: string): Promise<RunOut> {
  return unwrap(
    api.POST("/api/console/runs/{run_id}/stop", { params: { path: { run_id: runId } } }),
  );
}

// 202와 RunOut(queued)을 받는다. 재개된 런의 이벤트는 sessionEventsRequest로 다시 받는다.
export function provideRunInput(runId: string, body: RunInputBody): Promise<RunOut> {
  return requestJson<RunOut>("POST", `/api/console/runs/${enc(runId)}/input`, {
    json: { ...body, response_mode: "none" },
  });
}

export function uploadSessionFile(sessionId: string, file: File): Promise<SessionFileOut> {
  return uploadFile<SessionFileOut>(`/api/console/sessions/${enc(sessionId)}/uploads`, file);
}

// 다운로드는 항상 storage 복사본에서 받는다(설계 3.13).
export function sessionFileUrl(sessionId: string, fileId: string): string {
  return `/api/console/sessions/${enc(sessionId)}/files/${enc(fileId)}/download`;
}

export function messageStreamRequest(
  sessionId: string,
  body: { text: string; file_ids: string[] },
): Request {
  return new Request(`/api/console/sessions/${enc(sessionId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ ...body, response_mode: "streaming" }),
  });
}

// 진행 중인 런(또는 run_id로 고른 런)의 이벤트를 after 다음부터 다시 받는다.
export function sessionEventsRequest(
  sessionId: string,
  after: number,
  runId: string | null,
): Request {
  const path = withQuery(`/api/console/sessions/${enc(sessionId)}/events`, {
    after,
    run_id: runId,
  });
  return new Request(path, { headers: { Accept: "text/event-stream" } });
}

export function useSessionDetail(sessionId: string | null) {
  return useQuery({
    queryKey: sessionKeys.detail(sessionId ?? ""),
    queryFn: () => fetchSessionDetail(sessionId!),
    enabled: Boolean(sessionId),
  });
}

// 작업공간 상태(경로·디스크·큰 폴더·실행 중 프로세스). 패널은 M9에서 붙인다.
export function useSessionWorkspace(
  sessionId: string | null,
  options: { refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: sessionKeys.workspace(sessionId ?? ""),
    queryFn: () =>
      unwrap(api.GET("/api/console/sessions/{session_id}/workspace", sessionPath(sessionId!))),
    enabled: Boolean(sessionId),
    refetchInterval: options.refetchInterval,
  });
}

export const consoleSessionApi = {
  getSession: fetchSessionDetail,
  listMessages: (sessionId: string) => fetchSessionMessages(sessionId),
  upload: uploadSessionFile,
  stop: stopRun,
  input: provideRunInput,
  patchSession,
  messageRequest: messageStreamRequest,
  eventsRequest: sessionEventsRequest,
  fetch: (request: Request) => apiFetch(request),
};
