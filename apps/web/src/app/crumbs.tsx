import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api, unwrap } from "@/api/client";
import { useAgent } from "@/features/agents/api";

type CommonKey =
  "nav.agents" | "nav.workflows" | "nav.skills" | "nav.logs" | "nav.settings" | "nav.account";
type SettingsKey = "nav.llm" | "nav.workspace" | "nav.workspaces";

export function CommonCrumb({ k }: { k: CommonKey }) {
  const { t } = useTranslation();
  return <>{t(k)}</>;
}

export function SettingsCrumb({ k }: { k: SettingsKey }) {
  const { t } = useTranslation("settings");
  return <>{t(k)}</>;
}

// M9 설정 하위 화면(사용량, 허용 목록)
type SettingsExtraKey = "nav.usage" | "nav.httpAllowlist";

export function SettingsExtraCrumb({ k }: { k: SettingsExtraKey }) {
  const { t } = useTranslation("settings");
  return <>{t(k)}</>;
}

export function AgentLogsCrumb({ id }: { id: string }) {
  const { t } = useTranslation("logs");
  const agent = useAgent(id);
  const name = agent.data?.name ?? (agent.isError ? t("deletedAgent") : "…");
  return <>{t("agentCrumb", { name })}</>;
}

export function AgentCrumb({ id }: { id: string }) {
  const agent = useAgent(id);
  return <>{agent.data?.name ?? "…"}</>;
}

export function SessionCrumb({ sid }: { sid: string }) {
  const { t } = useTranslation("logs");
  return (
    <>
      {t("session.title")} <span className="font-mono text-xs">{sid.slice(0, 8)}</span>
    </>
  );
}

// 워크플로우 API 모듈(features/workflows/api)을 첫 화면 묶음에 끌어들이지 않으려고 여기서 직접 부른다.
// 쿼리 키는 workflowKeys.detail과 같아 편집기와 캐시를 나눠 쓴다.
function useWorkflowName(id: string) {
  return useQuery({
    queryKey: ["workflows", id],
    queryFn: () =>
      unwrap(
        api.GET("/api/console/workflows/{workflow_id}", { params: { path: { workflow_id: id } } }),
      ),
  });
}

export function WorkflowCrumb({ id }: { id: string }) {
  const workflow = useWorkflowName(id);
  return <>{workflow.data?.name ?? "…"}</>;
}

export function WorkflowLogsCrumb({ id }: { id: string }) {
  const { t } = useTranslation("logs");
  const workflow = useWorkflowName(id);
  const name = workflow.data?.name ?? (workflow.isError ? t("deletedWorkflow") : "…");
  return <>{t("workflowCrumb", { name })}</>;
}

export function WorkflowRunCrumb({ rid }: { rid: string }) {
  const { t } = useTranslation("logs");
  return (
    <>
      {t("workflowRunCrumb")} <span className="font-mono text-xs">{rid.slice(-8)}</span>
    </>
  );
}
