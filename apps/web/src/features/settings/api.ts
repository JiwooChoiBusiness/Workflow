import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { LlmStatus, WorkspaceOut, WorkspacePatch } from "@/api/types";
import { usageRange, type UsageQuery } from "./usage";

export const settingsKeys = {
  llm: ["llm"] as const,
  models: ["models"] as const,
  workspace: ["workspace"] as const,
  httpAllowlist: ["settings", "http-allowlist"] as const,
  usage: (query: UsageQuery) => ["stats", "usage", query] as const,
};

export function useHttpAllowlist() {
  return useQuery({
    queryKey: settingsKeys.httpAllowlist,
    queryFn: () => unwrap(api.GET("/api/console/settings/http-allowlist")),
    // 파일은 화면 밖에서 고친다. 돌아왔을 때 바로 보이게 캐시를 오래 두지 않는다.
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}

export function useUsage(query: UsageQuery) {
  return useQuery({
    queryKey: settingsKeys.usage(query),
    queryFn: () => {
      const range = usageRange(query.period, new Date());
      return unwrap(
        api.GET("/api/console/stats/usage", {
          params: {
            query: {
              group_by: query.groupBy,
              from: range.from,
              to: range.to,
              tz_offset_minutes: range.tzOffsetMinutes,
            },
          },
        }),
      );
    },
  });
}

function hasCooldown(status: LlmStatus | undefined): boolean {
  return Boolean(status?.keys.some((key) => key.cooldown_until));
}

export function useLlmStatus() {
  return useQuery({
    queryKey: settingsKeys.llm,
    queryFn: () => unwrap(api.GET("/api/console/llm")),
    // 쉬는 키가 있으면 풀리는 것이 보이도록 자주 다시 읽는다.
    refetchInterval: (query) => (hasCooldown(query.state.data) ? 5000 : false),
  });
}

export function useLlmTest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/console/llm/test")),
    onSettled: () => client.invalidateQueries({ queryKey: settingsKeys.llm }),
  });
}

export function useModels() {
  return useQuery({
    queryKey: settingsKeys.models,
    queryFn: () => unwrap(api.GET("/api/console/models")),
    staleTime: 5 * 60_000,
  });
}

export function useWorkspace() {
  return useQuery({
    queryKey: settingsKeys.workspace,
    queryFn: () => unwrap(api.GET("/api/console/workspace")),
  });
}

export function usePatchWorkspace() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkspacePatch) => unwrap(api.PATCH("/api/console/workspace", { body })),
    onSuccess: (workspace: WorkspaceOut) => {
      client.setQueryData(settingsKeys.workspace, workspace);
    },
  });
}
