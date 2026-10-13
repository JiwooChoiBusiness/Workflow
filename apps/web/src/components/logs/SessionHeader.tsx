import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValueList, Panel } from "@/components/ui/layout";
import { useApiKeys, useVersions } from "@/features/agents/api";
import { useAgentOrNull } from "@/features/logs/api";
import type { SessionDetail } from "@/features/logs/types";
import { formatDateTime, formatNumber } from "@/lib/format";
import { SourceBadge } from "./badges";

// 발행 이력에는 발행 버전만 있다. 미리보기 세션은 초안을 쓴다.
function VersionLabel({ session }: { session: SessionDetail }) {
  const { t } = useTranslation("logs");
  const versions = useVersions(session.agent_id);
  const version = versions.data?.find((item) => item.id === session.agent_version_id);
  if (version) return <>{t("header.versionNumber", { number: version.version_number })}</>;
  if (session.source === "preview") return <>{t("header.draft")}</>;
  return <span className="font-mono text-xs">{session.agent_version_id.slice(0, 8)}</span>;
}

function ApiKeyLabel({ agentId, keyId }: { agentId: string; keyId: string }) {
  const keys = useApiKeys(agentId);
  const key = keys.data?.find((item) => item.id === keyId);
  return key ? (
    <span>
      {key.name} <span className="font-mono text-xs text-ink-muted">{key.key_prefix}…</span>
    </span>
  ) : (
    <span className="font-mono text-xs">{keyId.slice(0, 8)}</span>
  );
}

// 세션 머리(설계 7.3 SessionHeader): 소스·API 키·외부 사용자·버전·승인 덮어쓰기.
export function SessionHeader({ session }: { session: SessionDetail }) {
  const { t } = useTranslation("logs");
  const agent = useAgentOrNull(session.agent_id);
  const agentDeleted = agent.isError;
  const items = [
    {
      label: t("header.agent"),
      value: agentDeleted ? (
        <span className="text-ink-subtle">{t("deletedAgent")}</span>
      ) : (
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <Link
            to={`/agents/${encodeURIComponent(session.agent_id)}`}
            className="text-accent hover:underline"
          >
            {agent.data?.name ?? "…"}
          </Link>
          <Link
            to={`/agents/${encodeURIComponent(session.agent_id)}/logs`}
            className="text-xs text-ink-muted hover:text-ink hover:underline"
          >
            {t("header.agentLogs")}
          </Link>
        </span>
      ),
    },
    {
      label: t("header.version"),
      value: agentDeleted ? (
        <span className="font-mono text-xs">{session.agent_version_id.slice(0, 8)}</span>
      ) : (
        <VersionLabel session={session} />
      ),
    },
    { label: t("header.source"), value: <SourceBadge source={session.source} /> },
    ...(session.api_key_id
      ? [
          {
            label: t("header.apiKey"),
            value: agentDeleted ? (
              <span className="font-mono text-xs">{session.api_key_id.slice(0, 8)}</span>
            ) : (
              <ApiKeyLabel agentId={session.agent_id} keyId={session.api_key_id} />
            ),
          },
        ]
      : []),
    ...(session.external_user
      ? [{ label: t("header.externalUser"), value: session.external_user }]
      : []),
    {
      label: t("header.approval"),
      value:
        session.approval_override === "off" ? (
          <Badge tone="warn">{t("autoApprove")}</Badge>
        ) : (
          <span className="text-ink-muted">{t("header.approvalFromAgent")}</span>
        ),
    },
    { label: t("header.messages"), value: formatNumber(session.message_count) },
    { label: t("header.created"), value: formatDateTime(session.created_at) },
    { label: t("header.lastActivity"), value: formatDateTime(session.last_message_at) },
    {
      label: t("header.id"),
      value: (
        <span className="flex items-center gap-1">
          <span className="font-mono text-xs">{session.id}</span>
          <CopyButton value={session.id} size="iconSm" variant="ghost" />
        </span>
      ),
    },
  ];
  return (
    <Panel>
      <KeyValueList
        items={items}
        className="grid-cols-[max-content_1fr] sm:grid-cols-[max-content_1fr_max-content_1fr]"
      />
    </Panel>
  );
}
