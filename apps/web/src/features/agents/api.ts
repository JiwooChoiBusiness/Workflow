import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { uploadFile } from "@/api/http";
import type {
  AgentCreate,
  AgentFileOut,
  AgentOut,
  AgentPatch,
  AgentVersionOut,
  ApiKeyCreate,
  DraftUpdate,
  SkillDetailOut,
} from "@/api/types";

export const agentKeys = {
  all: ["agents"] as const,
  detail: (id: string) => ["agents", id] as const,
  draft: (id: string) => ["agents", id, "draft"] as const,
  versions: (id: string) => ["agents", id, "versions"] as const,
  files: (id: string) => ["agents", id, "files"] as const,
  apiKeys: (id: string) => ["agents", id, "api-keys"] as const,
  skills: (id: string) => ["agents", id, "skills"] as const,
};

const agentPath = (id: string) => ({ params: { path: { agent_id: id } } });

export function useAgents() {
  return useQuery({
    queryKey: agentKeys.all,
    queryFn: async () => (await unwrap(api.GET("/api/console/agents"))).items,
  });
}

export function useAgent(id: string) {
  return useQuery({
    queryKey: agentKeys.detail(id),
    queryFn: () => unwrap(api.GET("/api/console/agents/{agent_id}", agentPath(id))),
  });
}

export function useCreateAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentCreate) => unwrap(api.POST("/api/console/agents", { body })),
    onSuccess: (agent) => {
      client.setQueryData(agentKeys.detail(agent.id), agent);
      void client.invalidateQueries({ queryKey: agentKeys.all, exact: true });
    },
  });
}

export function usePatchAgent(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AgentPatch) =>
      unwrap(api.PATCH("/api/console/agents/{agent_id}", { ...agentPath(id), body })),
    onSuccess: (agent) => {
      client.setQueryData(agentKeys.detail(id), agent);
      void client.invalidateQueries({ queryKey: agentKeys.all, exact: true });
    },
  });
}

export function useDeleteAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/console/agents/{agent_id}", agentPath(id))),
    onSuccess: (_data, id) => {
      client.removeQueries({ queryKey: agentKeys.detail(id) });
      void client.invalidateQueries({ queryKey: agentKeys.all, exact: true });
    },
  });
}

export function useAgentDraft(id: string) {
  return useQuery({
    queryKey: agentKeys.draft(id),
    queryFn: () => unwrap(api.GET("/api/console/agents/{agent_id}/draft", agentPath(id))),
    // 편집 중에 다시 읽어 폼을 덮지 않도록 자동 갱신을 끈다. 저장 응답으로 갱신한다.
    staleTime: Infinity,
  });
}

export function useSaveDraft(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DraftUpdate) =>
      unwrap(api.PUT("/api/console/agents/{agent_id}/draft", { ...agentPath(id), body })),
    onSuccess: (draft: AgentVersionOut) => {
      client.setQueryData(agentKeys.draft(id), draft);
      // 파일 목록의 in_draft가 바뀐다.
      void client.invalidateQueries({ queryKey: agentKeys.files(id) });
    },
  });
}

export function useVersions(id: string) {
  return useQuery({
    queryKey: agentKeys.versions(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/agents/{agent_id}/versions", agentPath(id)))).items,
  });
}

export function usePublish(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (changeNote: string | null) =>
      unwrap(
        api.POST("/api/console/agents/{agent_id}/publish", {
          ...agentPath(id),
          body: { change_note: changeNote },
        }),
      ),
    onSuccess: (published) => {
      client.setQueryData<AgentOut>(agentKeys.detail(id), published.agent);
      void client.invalidateQueries({ queryKey: agentKeys.versions(id) });
      void client.invalidateQueries({ queryKey: agentKeys.files(id) });
      void client.invalidateQueries({ queryKey: agentKeys.all, exact: true });
    },
  });
}

export function useRestoreVersion(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (versionId: string) =>
      unwrap(
        api.POST("/api/console/agents/{agent_id}/versions/{version_id}/restore", {
          params: { path: { agent_id: id, version_id: versionId } },
        }),
      ),
    onSuccess: (agent) => {
      client.setQueryData<AgentOut>(agentKeys.detail(id), agent);
      void client.invalidateQueries({ queryKey: agentKeys.versions(id) });
      void client.invalidateQueries({ queryKey: agentKeys.all, exact: true });
    },
  });
}

export function useAgentFiles(id: string) {
  return useQuery({
    queryKey: agentKeys.files(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/agents/{agent_id}/files", agentPath(id)))).items,
  });
}

// 올린 파일은 초안에 저절로 들어가지 않는다. 받은 id를 폼의 file_ids에 넣고 초안을 저장해야 한다.
export function useUploadAgentFile(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) =>
      uploadFile<AgentFileOut>(`/api/console/agents/${encodeURIComponent(id)}/files`, file),
    onSuccess: () => client.invalidateQueries({ queryKey: agentKeys.files(id) }),
  });
}

export function useDeleteAgentFile(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) =>
      unwrap(
        api.DELETE("/api/console/agents/{agent_id}/files/{file_id}", {
          params: { path: { agent_id: id, file_id: fileId } },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: agentKeys.files(id) }),
  });
}

export function useEmbeddedSkills(id: string) {
  return useQuery({
    queryKey: agentKeys.skills(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/agents/{agent_id}/skills", agentPath(id)))).items,
  });
}

// 임베디드 스킬도 초안에 저절로 들어가지 않는다. 받은 current_version_id를 skill_version_ids에 넣는다.
export function useUploadEmbeddedSkill(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) =>
      uploadFile<SkillDetailOut>(`/api/console/agents/${encodeURIComponent(id)}/skills`, file),
    onSuccess: () => client.invalidateQueries({ queryKey: agentKeys.skills(id) }),
  });
}

export function useApiKeys(id: string) {
  return useQuery({
    queryKey: agentKeys.apiKeys(id),
    queryFn: async () =>
      (await unwrap(api.GET("/api/console/agents/{agent_id}/api-keys", agentPath(id)))).items,
  });
}

export function useCreateApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ApiKeyCreate) =>
      unwrap(api.POST("/api/console/agents/{agent_id}/api-keys", { ...agentPath(id), body })),
    onSuccess: () => client.invalidateQueries({ queryKey: agentKeys.apiKeys(id) }),
  });
}

export function useRevokeApiKey(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (keyId: string) =>
      unwrap(
        api.DELETE("/api/console/agents/{agent_id}/api-keys/{key_id}", {
          params: { path: { agent_id: id, key_id: keyId } },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: agentKeys.apiKeys(id) }),
  });
}
