import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { apiFetch, networkError, toApiError, uploadFile } from "@/api/http";
import type { SkillDetailOut, SkillVersionOut } from "@/api/types";

export const skillKeys = {
  all: ["skills"] as const,
  detail: (skillId: string) => ["skills", skillId] as const,
  file: (skillId: string, versionId: string, path: string) =>
    ["skills", skillId, "versions", versionId, "files", path] as const,
};

export function useSkills() {
  return useQuery({
    queryKey: skillKeys.all,
    queryFn: async () => (await unwrap(api.GET("/api/console/skills"))).items,
  });
}

export function useSkill(skillId: string | null) {
  return useQuery({
    queryKey: skillKeys.detail(skillId ?? ""),
    enabled: Boolean(skillId),
    queryFn: () =>
      unwrap(
        api.GET("/api/console/skills/{skill_id}", {
          params: { path: { skill_id: skillId ?? "" } },
        }),
      ),
  });
}

export function useUploadSkill() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadFile<SkillDetailOut>("/api/console/skills", file),
    onSuccess: () => client.invalidateQueries({ queryKey: skillKeys.all }),
  });
}

export function useUploadSkillVersion() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ skillId, file }: { skillId: string; file: File }) =>
      uploadFile<SkillVersionOut>(
        `/api/console/skills/${encodeURIComponent(skillId)}/versions`,
        file,
      ),
    onSuccess: (_version, { skillId }) => {
      void client.invalidateQueries({ queryKey: skillKeys.all, exact: true });
      void client.invalidateQueries({ queryKey: skillKeys.detail(skillId) });
    },
  });
}

export function useDeleteSkill() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (skillId: string) =>
      unwrap(
        api.DELETE("/api/console/skills/{skill_id}", { params: { path: { skill_id: skillId } } }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: skillKeys.all }),
  });
}

export type SkillFileContent = { kind: "text"; text: string } | { kind: "binary"; url: string };

export function skillFileUrl(skillId: string, versionId: string, path: string): string {
  // 경로의 각 부분을 따로 인코딩해 /를 남긴다.
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `/api/console/skills/${encodeURIComponent(skillId)}/versions/${encodeURIComponent(versionId)}/files/${encoded}`;
}

// 서버는 글자 파일만 text/plain으로 주고 나머지는 내려받기(octet-stream)로 준다(설계 S9).
export function useSkillFile(skillId: string, versionId: string | null, path: string | null) {
  return useQuery({
    queryKey: skillKeys.file(skillId, versionId ?? "", path ?? ""),
    enabled: Boolean(versionId && path),
    staleTime: Infinity,
    queryFn: async (): Promise<SkillFileContent> => {
      const url = skillFileUrl(skillId, versionId ?? "", path ?? "");
      let response: Response;
      try {
        response = await apiFetch(url);
      } catch (cause) {
        throw networkError(cause);
      }
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        throw toApiError(response, body);
      }
      if ((response.headers.get("Content-Type") ?? "").startsWith("text/")) {
        return { kind: "text", text: await response.text() };
      }
      void response.body?.cancel();
      return { kind: "binary", url };
    },
  });
}
