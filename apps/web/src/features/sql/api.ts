/*
 * Text2SQL 데이터 소스 콘솔 API(설계 12.1). 목록·만들기·설정·삭제, 파일 올리기·지우기, 준비, 표·열 설명과 숨김,
 * SQL 시험과 기록, 스키마 내보내기, 예시 저장, DuckDB 런타임. 준비는 끝날 때까지 기다리는 요청이라 폴링하지
 * 않고, 런타임 설치만 202를 받은 뒤 끝날 때까지 2초마다 다시 읽는다.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { requestJson } from "@/api/http";
import type {
  SqlAnnotationsUpdate,
  SqlExampleCreate,
  SqlSourceCreate,
  SqlSourceOut,
  SqlSourcePatch,
} from "@/api/types";

export const RUNTIME_POLL_MS = 2000;
export const LOG_PAGE_SIZE = 10;

// 빵부스러기(app/crumbs.tsx)도 같은 키로 이름을 읽는다.
export const sqlKeys = {
  all: ["sql-sources"] as const,
  list: ["sql-sources", "list"] as const,
  detail: (id: string) => ["sql-sources", id] as const,
  logsAll: (id: string) => ["sql-sources", id, "query-logs"] as const,
  logs: (id: string, page: number) => ["sql-sources", id, "query-logs", page] as const,
  runtime: ["sql-runtime"] as const,
};

type Client = ReturnType<typeof useQueryClient>;

const sourcePath = (id: string) => ({ params: { path: { source_id: id } } });
const BASE = "/api/console/sql-sources";

function store(client: Client, source: SqlSourceOut) {
  client.setQueryData(sqlKeys.detail(source.id), source);
  void client.invalidateQueries({ queryKey: sqlKeys.list });
}

export function useSqlSources() {
  return useQuery({
    queryKey: sqlKeys.list,
    queryFn: async () => (await unwrap(api.GET("/api/console/sql-sources"))).items,
  });
}

// 노드 폼은 데이터 소스를 고르기 전(id가 빈 글)에도 부른다.
export function useSqlSource(id: string) {
  return useQuery({
    queryKey: sqlKeys.detail(id),
    enabled: id !== "",
    queryFn: () => unwrap(api.GET("/api/console/sql-sources/{source_id}", sourcePath(id))),
  });
}

export function useCreateSqlSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SqlSourceCreate) => unwrap(api.POST("/api/console/sql-sources", { body })),
    onSuccess: (source) => store(client, source),
  });
}

export function usePatchSqlSource(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SqlSourcePatch) =>
      unwrap(api.PATCH("/api/console/sql-sources/{source_id}", { ...sourcePath(id), body })),
    onSuccess: (source) => store(client, source),
  });
}

export function useDeleteSqlSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE("/api/console/sql-sources/{source_id}", sourcePath(id))),
    onSuccess: (_data, id) => {
      // 보고 있는 화면의 쿼리를 지우면 곧바로 다시 읽어 404가 난다. 보고 있지 않은 것만 지운다.
      client.removeQueries({ queryKey: sqlKeys.detail(id), type: "inactive" });
      void client.invalidateQueries({ queryKey: sqlKeys.list });
    },
  });
}

// 멀티파트 files 여러 개(app/api/console/sql_sources.py). sqlite 소스는 하나만 받고 올리면 바꾼다.
export function useUploadSqlFiles(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      return requestJson<SqlSourceOut>("POST", `${BASE}/${id}/files`, { body: form });
    },
    onSuccess: (source) => store(client, source),
  });
}

export function useDeleteSqlFile(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) =>
      unwrap(
        api.DELETE("/api/console/sql-sources/{source_id}/files/{file_id}", {
          params: { path: { source_id: id, file_id: fileId } },
        }),
      ),
    onSuccess: (source) => store(client, source),
  });
}

export function usePrepareSqlSource(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/console/sql-sources/{source_id}/prepare", sourcePath(id))),
    onSuccess: (source) => store(client, source),
  });
}

export function useUpdateAnnotations(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SqlAnnotationsUpdate) =>
      unwrap(
        api.PATCH("/api/console/sql-sources/{source_id}/annotations", { ...sourcePath(id), body }),
      ),
    onSuccess: (source) => store(client, source),
  });
}

// 검사 거부와 실행 실패는 200의 ok=false로 온다. 준비 전·런타임 없음만 409 오류다.
export function useRunSqlQuery(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { sql: string; max_rows: number | null }) =>
      unwrap(api.POST("/api/console/sql-sources/{source_id}/query", { ...sourcePath(id), body })),
    onSettled: () => void client.invalidateQueries({ queryKey: sqlKeys.logsAll(id) }),
  });
}

export function useSqlQueryLogs(id: string, page: number) {
  return useQuery({
    queryKey: sqlKeys.logs(id, page),
    queryFn: () =>
      unwrap(
        api.GET("/api/console/sql-sources/{source_id}/query-logs", {
          params: { path: { source_id: id }, query: { page, limit: LOG_PAGE_SIZE } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useExportSchema(id: string) {
  return useMutation({
    mutationFn: (kbId: string) =>
      unwrap(
        api.POST("/api/console/sql-sources/{source_id}/export-schema", {
          ...sourcePath(id),
          body: { kb_id: kbId },
        }),
      ),
  });
}

export function useSaveSqlExample(id: string) {
  return useMutation({
    mutationFn: (body: SqlExampleCreate) =>
      unwrap(
        api.POST("/api/console/sql-sources/{source_id}/examples", { ...sourcePath(id), body }),
      ),
  });
}

// SQLite 소스는 런타임 없이 돌아 화면이 enabled를 끈다.
export function useSqlRuntime(enabled = true) {
  return useQuery({
    queryKey: sqlKeys.runtime,
    enabled,
    queryFn: () => unwrap(api.GET("/api/console/sql-runtime")),
    refetchInterval: (query) =>
      query.state.data?.state === "installing" ? RUNTIME_POLL_MS : false,
  });
}

// 202로 받아 뒤에서 받는다. 응답의 상태(installing)를 캐시에 넣어 폴링을 바로 시작한다.
export function useInstallSqlRuntime() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/console/sql-runtime/prepare")),
    onSuccess: (status) => client.setQueryData(sqlKeys.runtime, status),
  });
}
