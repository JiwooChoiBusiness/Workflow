import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type WorkspaceStatusOut = Schemas["WorkspaceStatusOut"];
export type WorkspaceOverview = Schemas["WorkspaceOverview"];
export type WorkspaceOverviewList = Schemas["WorkspaceOverviewList"];
export type RunningExec = Schemas["RunningExec"];

// 도구 프로세스가 도는 동안에는 경과 시간과 메모리 최고 요청치가 바뀌므로 자주 다시 읽는다. 상태
// 조회는 폴더 크기를 다시 재므로(최대 3초) 너무 자주 부르지 않는다.
const RUNNING_REFRESH_MS = 3000;
const LIST_REFRESH_MS = 5000;

export const workspaceKeys = {
  list: ["workspaces"] as const,
  session: (sid: string) => ["workspaces", "session", sid] as const,
};

const sessionPath = (sid: string) => ({ params: { path: { session_id: sid } } });

export function useWorkspaces() {
  return useQuery({
    queryKey: workspaceKeys.list,
    queryFn: () => unwrap(api.GET("/api/console/workspaces")),
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => (item.running ?? []).length > 0 || item.status === "busy",
      )
        ? LIST_REFRESH_MS
        : false,
  });
}

export function useSessionWorkspace(sid: string | null) {
  return useQuery({
    queryKey: workspaceKeys.session(sid ?? ""),
    enabled: sid !== null,
    queryFn: () =>
      unwrap(api.GET("/api/console/sessions/{session_id}/workspace", sessionPath(sid!))),
    refetchInterval: (query) =>
      query.state.data && (query.state.data.running ?? []).length > 0 ? RUNNING_REFRESH_MS : false,
  });
}

function useAfterChange(sid: string) {
  const client = useQueryClient();
  return (status?: WorkspaceStatusOut) => {
    if (status) client.setQueryData(workspaceKeys.session(sid), status);
    else void client.invalidateQueries({ queryKey: workspaceKeys.session(sid) });
    void client.invalidateQueries({ queryKey: workspaceKeys.list, exact: true });
  };
}

// exec_id를 비우면 그 작업공간에서 도는 도구 프로세스를 모두 끝낸다. 런은 멈추지 않고 그 도구
// 호출만 오류로 끝난다.
export function useKillWorkspaceProcesses(sid: string) {
  const done = useAfterChange(sid);
  return useMutation({
    mutationFn: (execId: string | null) =>
      unwrap(
        api.POST("/api/console/sessions/{session_id}/workspace/kill", {
          ...sessionPath(sid),
          body: { exec_id: execId },
        }),
      ),
    onSettled: () => done(),
  });
}

export function useCleanWorkspacePackages(sid: string) {
  const done = useAfterChange(sid);
  return useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/console/sessions/{session_id}/workspace/clean-packages", sessionPath(sid)),
      ),
    onSuccess: (status) => done(status),
    onError: () => done(),
  });
}

export function useResetWorkspace(sid: string) {
  const done = useAfterChange(sid);
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/console/sessions/{session_id}/workspace/reset", sessionPath(sid))),
    onSuccess: (status) => done(status),
    onError: () => done(),
  });
}
