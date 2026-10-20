/*
 * 챗플로우 대화의 콘솔 API(설계 6.1). 편집기 미리보기 대화(초안으로 돈다)와 실행 기록 화면의 대화 목록이
 * 쓴다. 메시지를 보내면 런이 생기고, 그 런의 이벤트·중지·응답은 워크플로우 런 경로(/wf-runs)를 쓴다.
 */
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { WfConversationSource, WfMessageCreate } from "@/api/types";
import type { ChatflowApi } from "./chatflowSession";
import { stopWfRun, uploadWorkflowFile, workflowKeys } from "./api";

const enc = encodeURIComponent;
const conversationPath = (id: string) => ({ params: { path: { conversation_id: id } } });

export const chatKeys = {
  all: ["wf-conversations"] as const,
  preview: (workflowId: string) => [...workflowKeys.detail(workflowId), "preview-chats"] as const,
  list: (workflowId: string, source: WfConversationSource | null) =>
    ["wf-conversations", "list", workflowId, source ?? "all"] as const,
  messages: (conversationId: string) => ["wf-conversations", conversationId, "messages"] as const,
  variables: (conversationId: string) => ["wf-conversations", conversationId, "variables"] as const,
};

const PREVIEW_PAGE = 50;
const LIST_PAGE = 50;

export function usePreviewConversations(workflowId: string) {
  return useQuery({
    queryKey: chatKeys.preview(workflowId),
    queryFn: async () =>
      (
        await unwrap(
          api.GET("/api/console/workflows/{workflow_id}/preview/conversations", {
            params: { path: { workflow_id: workflowId }, query: { limit: PREVIEW_PAGE } },
          }),
        )
      ).items,
  });
}

export async function createPreviewConversation(workflowId: string): Promise<string> {
  const created = await unwrap(
    api.POST("/api/console/workflows/{workflow_id}/preview/conversations", {
      params: { path: { workflow_id: workflowId } },
      body: {},
    }),
  );
  return created.id;
}

export function renameConversation(conversationId: string, title: string) {
  return unwrap(
    api.PATCH("/api/console/wf-conversations/{conversation_id}", {
      ...conversationPath(conversationId),
      body: { title },
    }),
  );
}

export function deleteConversation(conversationId: string) {
  return unwrap(
    api.DELETE("/api/console/wf-conversations/{conversation_id}", conversationPath(conversationId)),
  );
}

export function fetchConversationMessages(conversationId: string) {
  return unwrap(
    api.GET("/api/console/wf-conversations/{conversation_id}/messages", {
      params: { path: { conversation_id: conversationId }, query: { limit: 100 } },
    }),
  );
}

// 대화 변수의 지금 값. 성공한 턴이 끝날 때만 바뀐다.
export function useConversationVariables(conversationId: string | null) {
  return useQuery({
    queryKey: chatKeys.variables(conversationId ?? ""),
    enabled: conversationId !== null,
    queryFn: async () =>
      (
        await unwrap(
          api.GET(
            "/api/console/wf-conversations/{conversation_id}/variables",
            conversationPath(conversationId ?? ""),
          ),
        )
      ).items,
  });
}

// 실행 기록 화면의 대화 탭. 최근 대화 순으로 50개씩.
export function useWorkflowConversations(workflowId: string, source: WfConversationSource | null) {
  return useInfiniteQuery({
    queryKey: chatKeys.list(workflowId, source),
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/console/wf-conversations", {
          params: {
            query: {
              workflow_id: workflowId,
              source: source ?? undefined,
              limit: LIST_PAGE,
              offset: pageParam,
            },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_offset ?? undefined,
    placeholderData: keepPreviousData,
  });
}

export function useConversationMessages(conversationId: string | null) {
  return useQuery({
    queryKey: chatKeys.messages(conversationId ?? ""),
    enabled: conversationId !== null,
    queryFn: () => fetchConversationMessages(conversationId ?? ""),
  });
}

export function conversationMessageRequest(conversationId: string, body: WfMessageCreate): Request {
  return new Request(`/api/console/wf-conversations/${enc(conversationId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
}

export function wfConversationFileUrl(conversationId: string, fileId: string): string {
  return `/api/console/wf-conversations/${enc(conversationId)}/files/${enc(fileId)}/download`;
}

export function consoleChatApi(workflowId: string): ChatflowApi {
  return {
    getConversation: (conversationId) =>
      unwrap(
        api.GET(
          "/api/console/wf-conversations/{conversation_id}",
          conversationPath(conversationId),
        ),
      ),
    listMessages: fetchConversationMessages,
    messageRequest: conversationMessageRequest,
    upload: (file) => uploadWorkflowFile(workflowId, file),
    stop: stopWfRun,
    fileUrl: wfConversationFileUrl,
  };
}
