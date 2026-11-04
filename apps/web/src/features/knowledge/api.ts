/*
 * 지식 베이스 콘솔 API(설계 12.1). KB·통계·기본값, 문서(올리기·미리보기·상태 조작·재색인), 청크와 자식 청크,
 * 메타데이터, 검색 시험·질의 기록, 평가, KB API 키. 진행 중 문서가 있을 때만 문서 표와 KB를 2초마다 다시 읽는다.
 */
import {
  keepPreviousData,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, unwrap } from "@/api/client";
import { requestJson } from "@/api/http";
import type {
  ApiKeyCreate,
  DisplayStatus,
  DocumentMetadataUpdate,
  DocumentPreview,
  DocumentStatusResult,
  EvalCaseCreate,
  EvalImportResult,
  EvalRunOut,
  KbDocumentOut,
  KbDocumentsCreated,
  KnowledgeBaseCreate,
  KnowledgeBasePatch,
  MetadataFieldList,
  MetadataFieldOut,
  ProcessRule,
  RetrievalSettings,
  RetrieveRequest,
  SegmentPatch,
} from "@/api/types";
import {
  batchPollInterval,
  documentsPollInterval,
  isInProgress,
  POLL_MS,
  type DocumentAction,
} from "./documentStatus";

export interface DocumentQuery {
  status?: DisplayStatus;
  keyword?: string;
  page: number;
  limit: number;
}

export interface SegmentQuery {
  keyword?: string;
  enabled?: boolean;
  page: number;
  limit: number;
}

export const knowledgeKeys = {
  all: ["knowledge"] as const,
  list: ["knowledge", "list"] as const,
  defaults: ["knowledge", "defaults"] as const,
  detail: (id: string) => ["knowledge", id] as const,
  stats: (id: string) => ["knowledge", id, "stats"] as const,
  documentsAll: (id: string) => ["knowledge", id, "documents"] as const,
  documents: (id: string, query: DocumentQuery) => ["knowledge", id, "documents", query] as const,
  document: (id: string, docId: string) => ["knowledge", id, "document", docId] as const,
  segmentsAll: (id: string, docId: string) =>
    ["knowledge", id, "document", docId, "segments"] as const,
  segments: (id: string, docId: string, query: SegmentQuery) =>
    ["knowledge", id, "document", docId, "segments", query] as const,
  batch: (id: string, batch: string) => ["knowledge", id, "batch", batch] as const,
  metadata: (id: string) => ["knowledge", id, "metadata"] as const,
  queryLogs: (id: string, page: number) => ["knowledge", id, "query-logs", page] as const,
  evalCases: (id: string) => ["knowledge", id, "eval", "cases"] as const,
  evalRuns: (id: string) => ["knowledge", id, "eval", "runs"] as const,
  evalRun: (id: string, runId: string) => ["knowledge", id, "eval", "runs", runId] as const,
  apiKeys: (id: string) => ["knowledge", id, "api-keys"] as const,
};

type Client = ReturnType<typeof useQueryClient>;

const kbPath = (id: string) => ({ params: { path: { kb_id: id } } });
const docPath = (id: string, docId: string) => ({
  params: { path: { kb_id: id, document_id: docId } },
});
const segmentPath = (id: string, docId: string, segmentId: string) => ({
  params: { path: { kb_id: id, document_id: docId, segment_id: segmentId } },
});

const BASE = "/api/console/knowledge";

// 문서·청크가 바뀌면 KB(문서·청크 수, 색인 중 수)와 통계도 바뀐다.
function refreshKb(client: Client, id: string) {
  void client.invalidateQueries({ queryKey: knowledgeKeys.detail(id), exact: true });
  void client.invalidateQueries({ queryKey: knowledgeKeys.stats(id) });
  void client.invalidateQueries({ queryKey: knowledgeKeys.list });
}

// 지운 문서(deleted)의 쿼리는 다시 읽으면 404라 건드리지 않는다. 화면을 떠나면 캐시에서 빠진다.
function refreshDocuments(client: Client, id: string, deleted?: ReadonlySet<string>) {
  void client.invalidateQueries({ queryKey: knowledgeKeys.documentsAll(id) });
  void client.invalidateQueries({
    queryKey: [...knowledgeKeys.detail(id), "document"],
    predicate: (query) => !deleted?.has(String(query.queryKey[3])),
  });
  refreshKb(client, id);
}

export function useKnowledgeBases() {
  return useQuery({
    queryKey: knowledgeKeys.list,
    queryFn: async () => (await unwrap(api.GET("/api/console/knowledge"))).items,
  });
}

// 하루 사용량이 들어 있어 화면을 열 때마다 새로 읽는다.
export function useKnowledgeDefaults() {
  return useQuery({
    queryKey: knowledgeKeys.defaults,
    queryFn: () => unwrap(api.GET("/api/console/knowledge-defaults")),
    staleTime: 30_000,
  });
}

export function useKnowledgeBase(id: string) {
  return useQuery({
    queryKey: knowledgeKeys.detail(id),
    queryFn: () => unwrap(api.GET("/api/console/knowledge/{kb_id}", kbPath(id))),
    refetchInterval: (query) =>
      (query.state.data?.indexing_document_count ?? 0) > 0 ? POLL_MS : false,
  });
}

// 통계는 문서 수를 상태별로 다시 세고 색인 파일까지 보므로 색인 중에도 5초에 한 번만 읽는다.
export function useKnowledgeStats(id: string, active: boolean) {
  return useQuery({
    queryKey: knowledgeKeys.stats(id),
    queryFn: () => unwrap(api.GET("/api/console/knowledge/{kb_id}/stats", kbPath(id))),
    refetchInterval: active ? 5000 : false,
  });
}

export function useCreateKnowledgeBase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: KnowledgeBaseCreate) => unwrap(api.POST("/api/console/knowledge", { body })),
    onSuccess: (kb) => {
      client.setQueryData(knowledgeKeys.detail(kb.id), kb);
      void client.invalidateQueries({ queryKey: knowledgeKeys.list });
    },
  });
}

export function usePatchKnowledgeBase(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<KnowledgeBasePatch>) =>
      unwrap(
        api.PATCH("/api/console/knowledge/{kb_id}", {
          ...kbPath(id),
          body: { confirm_reembed: false, ...body },
        }),
      ),
    onSuccess: (kb) => {
      client.setQueryData(knowledgeKeys.detail(id), kb);
      void client.invalidateQueries({ queryKey: knowledgeKeys.stats(id) });
      void client.invalidateQueries({ queryKey: knowledgeKeys.documentsAll(id) });
      void client.invalidateQueries({ queryKey: knowledgeKeys.list });
    },
  });
}

export function useDeleteKnowledgeBase() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/console/knowledge/{kb_id}", kbPath(id))),
    onSuccess: (_data, id) => {
      // 지금 화면이 보고 있는 쿼리(KB 화면, 빵부스러기)를 지우면 곧바로 다시 읽어 404가 난다.
      // 보고 있지 않은 것만 지우고, 나머지는 화면을 떠난 뒤 캐시에서 빠진다.
      client.removeQueries({ queryKey: knowledgeKeys.detail(id), type: "inactive" });
      void client.invalidateQueries({ queryKey: knowledgeKeys.list });
    },
  });
}

// paused면 폴링하지 않는다. 업로드 대화상자가 그 묶음의 진행을 따로 읽는 동안 겹치지 않게 한다.
export function useDocuments(
  id: string,
  query: DocumentQuery,
  kbInProgress: number,
  paused = false,
) {
  return useQuery({
    queryKey: knowledgeKeys.documents(id, query),
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/documents", {
          params: {
            path: { kb_id: id },
            query: {
              status: query.status ?? null,
              keyword: query.keyword || null,
              page: query.page,
              limit: query.limit,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
    refetchInterval: (state) =>
      paused ? false : documentsPollInterval(state.state.data?.items, kbInProgress),
  });
}

// 평가 문항의 정답 문서 고르기. 이름으로 찾고 폴링하지 않는다.
export function useDocumentSearch(id: string, keyword: string, enabled: boolean) {
  return useQuery({
    queryKey: [...knowledgeKeys.documentsAll(id), "search", keyword] as const,
    enabled,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/documents", {
          params: {
            path: { kb_id: id },
            query: { keyword: keyword.trim() || null, page: 1, limit: 10 },
          },
        }),
      ),
    placeholderData: keepPreviousData,
    select: (data) => data.items,
  });
}

export function useDocument(id: string, docId: string) {
  return useQuery({
    queryKey: knowledgeKeys.document(id, docId),
    queryFn: () =>
      unwrap(api.GET("/api/console/knowledge/{kb_id}/documents/{document_id}", docPath(id, docId))),
    refetchInterval: (query) =>
      query.state.data && isInProgress(query.state.data) ? POLL_MS : false,
  });
}

// 올린 묶음의 진행(콘솔 indexing-status). 하나라도 진행 중이면 2초마다 다시 읽는다.
export function useBatchStatus(id: string, batch: string | null) {
  return useQuery({
    queryKey: knowledgeKeys.batch(id, batch ?? ""),
    enabled: batch !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/documents/indexing-status", {
          params: { path: { kb_id: id }, query: { batch: batch ?? "" } },
        }),
      ),
    refetchInterval: (query) => batchPollInterval(query.state.data?.data),
  });
}

// 색인이 끝나는 순간(진행 중 문서 수가 0이 되는 때) 통계와 문서 표를 한 번 더 읽는다. 폴링은 진행 중일
// 때만 돌아 마지막 상태를 놓칠 수 있다.
export function useRefreshWhenSettled(id: string, inProgress: number) {
  const client = useQueryClient();
  const previous = useRef(inProgress);
  useEffect(() => {
    if (previous.current > 0 && inProgress === 0) {
      void client.invalidateQueries({ queryKey: knowledgeKeys.stats(id) });
      void client.invalidateQueries({ queryKey: knowledgeKeys.documentsAll(id) });
    }
    previous.current = inProgress;
  }, [client, id, inProgress]);
}

// 청크 목록은 따로 폴링하지 않고, 2초마다 읽는 문서의 상태·진행이 바뀔 때만 다시 읽는다.
export function useSegmentsFollowDocument(
  id: string,
  document: Pick<KbDocumentOut, "id" | "indexing_status" | "completed_segments" | "total_segments">,
) {
  const client = useQueryClient();
  const signature = `${document.indexing_status}:${document.completed_segments}:${document.total_segments}`;
  const previous = useRef(signature);
  useEffect(() => {
    if (previous.current === signature) return;
    previous.current = signature;
    void client.invalidateQueries({ queryKey: knowledgeKeys.segmentsAll(id, document.id) });
  }, [client, id, document.id, signature]);
}

export interface UploadOptions {
  process_rule?: ProcessRule;
  allow_duplicate?: boolean;
  extract_mode?: "text" | "model";
}

export type UploadInput =
  | { kind: "files"; files: File[]; options: UploadOptions }
  | { kind: "text"; name: string; text: string; options: UploadOptions };

// 멀티파트는 files 여러 개와 JSON 옵션 칸 data다(app/api/console/knowledge_documents.py).
export function uploadBody(input: UploadInput): { body?: FormData; json?: unknown } {
  if (input.kind === "text") {
    const { process_rule, allow_duplicate } = input.options;
    return {
      json: {
        name: input.name,
        text: input.text,
        ...(process_rule ? { process_rule } : {}),
        ...(allow_duplicate ? { allow_duplicate } : {}),
      },
    };
  }
  const form = new FormData();
  for (const file of input.files) form.append("files", file);
  const options = Object.fromEntries(
    Object.entries(input.options).filter(([, value]) => value !== undefined && value !== false),
  );
  if (Object.keys(options).length > 0) form.append("data", JSON.stringify(options));
  return { body: form };
}

export function useUploadDocuments(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: UploadInput) =>
      requestJson<KbDocumentsCreated>("POST", `${BASE}/${id}/documents`, uploadBody(input)),
    onSuccess: () => refreshDocuments(client, id),
  });
}

export type PreviewInput =
  | { kind: "file"; file: File; processRule?: ProcessRule }
  | { kind: "text"; text: string; processRule?: ProcessRule };

export function previewBody(input: PreviewInput): { body?: FormData; json?: unknown } {
  if (input.kind === "text") {
    return {
      json: { text: input.text, ...(input.processRule ? { process_rule: input.processRule } : {}) },
    };
  }
  const form = new FormData();
  form.append("file", input.file);
  if (input.processRule) form.append("data", JSON.stringify({ process_rule: input.processRule }));
  return { body: form };
}

export function usePreviewDocument(id: string) {
  return useMutation({
    mutationFn: (input: PreviewInput) =>
      requestJson<DocumentPreview>("POST", `${BASE}/${id}/documents/preview`, previewBody(input)),
  });
}

export function useDocumentAction(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ action, ids }: { action: DocumentAction; ids: string[] }) =>
      requestJson<DocumentStatusResult>("POST", `${BASE}/${id}/documents/status/${action}`, {
        json: { document_ids: ids },
      }),
    onSettled: () => refreshDocuments(client, id),
  });
}

export function useReindexDocument(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ docId, mode }: { docId: string; mode?: "text" | "model" }) =>
      unwrap(
        api.POST("/api/console/knowledge/{kb_id}/documents/{document_id}/reindex", {
          ...docPath(id, docId),
          body: { extract_mode: mode ?? null },
        }),
      ),
    onSuccess: (document) => {
      client.setQueryData(knowledgeKeys.document(id, document.id), document);
      refreshDocuments(client, id);
    },
  });
}

export function useRenameDocument(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ docId, name }: { docId: string; name: string }) =>
      unwrap(
        api.PATCH("/api/console/knowledge/{kb_id}/documents/{document_id}", {
          ...docPath(id, docId),
          body: { name },
        }),
      ),
    onSuccess: (document) => {
      client.setQueryData<KbDocumentOut>(knowledgeKeys.document(id, document.id), document);
      void client.invalidateQueries({ queryKey: knowledgeKeys.documentsAll(id) });
    },
  });
}

export function useDeleteDocuments(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (docIds: string[]) => {
      for (const docId of docIds) {
        await unwrap(
          api.DELETE("/api/console/knowledge/{kb_id}/documents/{document_id}", docPath(id, docId)),
        );
      }
    },
    onSettled: (_data, _error, docIds) => refreshDocuments(client, id, new Set(docIds)),
  });
}

export function useSegments(id: string, docId: string, query: SegmentQuery) {
  return useQuery({
    queryKey: knowledgeKeys.segments(id, docId, query),
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/documents/{document_id}/segments", {
          params: {
            path: { kb_id: id, document_id: docId },
            query: {
              keyword: query.keyword || null,
              enabled: query.enabled ?? null,
              page: query.page,
              limit: query.limit,
            },
          },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

function refreshSegments(client: Client, id: string, docId: string) {
  void client.invalidateQueries({ queryKey: knowledgeKeys.segmentsAll(id, docId) });
  void client.invalidateQueries({ queryKey: knowledgeKeys.document(id, docId), exact: true });
  refreshKb(client, id);
}

export function useCreateSegment(id: string, docId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { content: string; answer?: string | null }) =>
      unwrap(
        api.POST("/api/console/knowledge/{kb_id}/documents/{document_id}/segments", {
          ...docPath(id, docId),
          body,
        }),
      ),
    onSuccess: () => refreshSegments(client, id, docId),
  });
}

export function usePatchSegment(id: string, docId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ segmentId, body }: { segmentId: string; body: SegmentPatch }) =>
      unwrap(
        api.PATCH("/api/console/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}", {
          ...segmentPath(id, docId, segmentId),
          body,
        }),
      ),
    onSuccess: () => refreshSegments(client, id, docId),
  });
}

export function useDeleteSegment(id: string, docId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (segmentId: string) =>
      unwrap(
        api.DELETE(
          "/api/console/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}",
          segmentPath(id, docId, segmentId),
        ),
      ),
    onSuccess: () => refreshSegments(client, id, docId),
  });
}

export function useSaveChild(id: string, docId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      segmentId,
      childId,
      content,
    }: {
      segmentId: string;
      childId?: string;
      content: string;
    }) =>
      childId
        ? unwrap(
            api.PATCH(
              "/api/console/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children/{child_id}",
              {
                params: {
                  path: { kb_id: id, document_id: docId, segment_id: segmentId, child_id: childId },
                },
                body: { content },
              },
            ),
          )
        : unwrap(
            api.POST(
              "/api/console/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children",
              { ...segmentPath(id, docId, segmentId), body: { content } },
            ),
          ),
    onSuccess: () => refreshSegments(client, id, docId),
  });
}

export function useDeleteChild(id: string, docId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ segmentId, childId }: { segmentId: string; childId: string }) =>
      unwrap(
        api.DELETE(
          "/api/console/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children/{child_id}",
          {
            params: {
              path: { kb_id: id, document_id: docId, segment_id: segmentId, child_id: childId },
            },
          },
        ),
      ),
    onSuccess: () => refreshSegments(client, id, docId),
  });
}

function fetchMetadataFields(id: string) {
  return unwrap(api.GET("/api/console/knowledge/{kb_id}/metadata", kbPath(id)));
}

export function useMetadataFields(id: string) {
  return useQuery({
    queryKey: knowledgeKeys.metadata(id),
    queryFn: () => fetchMetadataFields(id),
  });
}

// 모두 읽었을 때만 목록을 준다. 일부만 온 목록으로 공통 필드를 셈하면 다 올 때까지 집합이 줄었다 늘었다
// 한다. combine이 모듈 함수라야 쿼리 결과가 바뀔 때만 다시 셈하고, 값이 같으면 같은 객체를 돌려준다.
function combineFieldLists(results: UseQueryResult<MetadataFieldList>[]): {
  lists: MetadataFieldOut[][] | null;
  error: Error | null;
} {
  return {
    lists: results.every((result) => result.isSuccess)
      ? results.map((result) => result.data?.items ?? [])
      : null,
    error: results.find((result) => result.isError)?.error ?? null,
  };
}

// 노드 폼이 고른 지식 베이스들의 필드를 함께 읽어 공통 필드만 보인다(서버는 저장할 때만 알린다).
export function useMetadataFieldLists(ids: readonly string[]) {
  const client = useQueryClient();
  const state = useQueries({
    queries: ids.map((id) => ({
      queryKey: knowledgeKeys.metadata(id),
      queryFn: () => fetchMetadataFields(id),
    })),
    combine: combineFieldLists,
  });
  return {
    ...state,
    retry: () => {
      for (const id of ids) void client.refetchQueries({ queryKey: knowledgeKeys.metadata(id) });
    },
  };
}

function refreshMetadata(client: Client, id: string) {
  void client.invalidateQueries({ queryKey: knowledgeKeys.metadata(id) });
  void client.invalidateQueries({ queryKey: knowledgeKeys.documentsAll(id) });
  void client.invalidateQueries({ queryKey: [...knowledgeKeys.detail(id), "document"] });
}

export function useCreateField(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; type: "string" | "number" | "time" }) =>
      unwrap(api.POST("/api/console/knowledge/{kb_id}/metadata", { ...kbPath(id), body })),
    onSuccess: () => refreshMetadata(client, id),
  });
}

export function useRenameField(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ fieldId, name }: { fieldId: string; name: string }) =>
      unwrap(
        api.PATCH("/api/console/knowledge/{kb_id}/metadata/{field_id}", {
          params: { path: { kb_id: id, field_id: fieldId } },
          body: { name },
        }),
      ),
    onSuccess: () => refreshMetadata(client, id),
  });
}

export function useDeleteField(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (fieldId: string) =>
      unwrap(
        api.DELETE("/api/console/knowledge/{kb_id}/metadata/{field_id}", {
          params: { path: { kb_id: id, field_id: fieldId } },
        }),
      ),
    onSuccess: () => refreshMetadata(client, id),
  });
}

export function useToggleBuiltIn(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (enable: boolean) =>
      unwrap(
        api.POST("/api/console/knowledge/{kb_id}/metadata/built-in/{action}", {
          params: { path: { kb_id: id, action: enable ? "enable" : "disable" } },
        }),
      ),
    onSuccess: () => {
      refreshMetadata(client, id);
      void client.invalidateQueries({ queryKey: knowledgeKeys.detail(id), exact: true });
    },
  });
}

export function useUpdateDocumentMetadata(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DocumentMetadataUpdate) =>
      unwrap(
        api.POST("/api/console/knowledge/{kb_id}/documents/metadata", { ...kbPath(id), body }),
      ),
    onSuccess: () => refreshMetadata(client, id),
  });
}

export function useRetrieve(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: RetrieveRequest) =>
      unwrap(api.POST("/api/console/knowledge/{kb_id}/retrieve", { ...kbPath(id), body })),
    onSettled: () =>
      void client.invalidateQueries({ queryKey: [...knowledgeKeys.detail(id), "query-logs"] }),
  });
}

export function useQueryLogs(id: string, page: number) {
  return useQuery({
    queryKey: knowledgeKeys.queryLogs(id, page),
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/query-logs", {
          params: { path: { kb_id: id }, query: { page, limit: 10 } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useEvalCases(id: string) {
  return useQuery({
    queryKey: knowledgeKeys.evalCases(id),
    queryFn: () => unwrap(api.GET("/api/console/knowledge/{kb_id}/eval/cases", kbPath(id))),
  });
}

export function useCreateEvalCase(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: EvalCaseCreate) =>
      unwrap(api.POST("/api/console/knowledge/{kb_id}/eval/cases", { ...kbPath(id), body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: knowledgeKeys.evalCases(id) }),
  });
}

export function useDeleteEvalCase(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (caseId: string) =>
      unwrap(
        api.DELETE("/api/console/knowledge/{kb_id}/eval/cases/{case_id}", {
          params: { path: { kb_id: id, case_id: caseId } },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: knowledgeKeys.evalCases(id) }),
  });
}

export function useImportEvalCases(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ csv, file, replace }: { csv?: string; file?: File; replace: boolean }) => {
      if (file) {
        const form = new FormData();
        form.append("file", file);
        return requestJson<EvalImportResult>("POST", `${BASE}/${id}/eval/cases/import`, {
          body: form,
          query: { replace },
        });
      }
      return requestJson<EvalImportResult>("POST", `${BASE}/${id}/eval/cases/import`, {
        json: { csv: csv ?? "", replace },
      });
    },
    onSettled: () => void client.invalidateQueries({ queryKey: knowledgeKeys.evalCases(id) }),
  });
}

export function useRunEval(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { retrieval_model: RetrievalSettings | null; save: boolean }) =>
      unwrap(api.POST("/api/console/knowledge/{kb_id}/eval/run", { ...kbPath(id), body })),
    onSuccess: (run) => {
      if (run.id) client.setQueryData(knowledgeKeys.evalRun(id, run.id), run);
      void client.invalidateQueries({ queryKey: knowledgeKeys.evalRuns(id) });
    },
  });
}

export function useEvalRuns(id: string) {
  return useQuery({
    queryKey: knowledgeKeys.evalRuns(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/knowledge/{kb_id}/eval/runs", kbPath(id)))).items,
  });
}

export function useEvalRun(id: string, runId: string | null) {
  return useQuery<EvalRunOut>({
    queryKey: knowledgeKeys.evalRun(id, runId ?? ""),
    enabled: runId !== null,
    queryFn: () =>
      unwrap(
        api.GET("/api/console/knowledge/{kb_id}/eval/runs/{run_id}", {
          params: { path: { kb_id: id, run_id: runId ?? "" } },
        }),
      ),
    staleTime: Infinity,
  });
}

export function useKbApiKeys(id: string) {
  return useQuery({
    queryKey: knowledgeKeys.apiKeys(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/knowledge/{kb_id}/api-keys", kbPath(id)))).items,
  });
}

export function useCreateKbApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ApiKeyCreate) =>
      unwrap(api.POST("/api/console/knowledge/{kb_id}/api-keys", { ...kbPath(id), body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: knowledgeKeys.apiKeys(id) }),
  });
}

export function useRevokeKbApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (keyId: string) =>
      unwrap(
        api.DELETE("/api/console/knowledge/{kb_id}/api-keys/{key_id}", {
          params: { path: { kb_id: id, key_id: keyId } },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: knowledgeKeys.apiKeys(id) }),
  });
}
