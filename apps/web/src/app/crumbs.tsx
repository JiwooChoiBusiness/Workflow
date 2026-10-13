import { useTranslation } from "react-i18next";
import { useAgent } from "@/features/agents/api";

type CommonKey = "nav.agents" | "nav.skills" | "nav.logs" | "nav.settings" | "nav.account";
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
