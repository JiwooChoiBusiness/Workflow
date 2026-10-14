import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { AgentOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Panel } from "@/components/ui/layout";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { usePatchAgent } from "@/features/agents/api";
import { errorMessage } from "@/lib/errors";
import { ApiExampleList, serverOrigin } from "./ApiExamples";
import { ApiKeyPanel } from "./ApiKeyPanel";

function webAppPath(slug: string): string {
  return `/apps/${encodeURIComponent(slug)}`;
}

// 웹앱(/apps/<slug>) 공개와 링크. 웹앱은 발행되고 공개(webapp_enabled)한 에이전트만 열린다(설계 6.3).
function WebAppPanel({ agent }: { agent: AgentOut }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const patch = usePatchAgent(agent.id);
  const published = Boolean(agent.published_version_id);
  const url = `${serverOrigin()}${webAppPath(agent.slug)}`;
  return (
    <Panel title={t("webapp.title")} description={t("webapp.description")}>
      {!published ? (
        <p className="text-sm text-ink-muted">{t("webapp.publishFirst")}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <Switch
              id="webapp-enabled"
              checked={agent.webapp_enabled}
              disabled={patch.isPending}
              aria-describedby="webapp-enabled-desc"
              onCheckedChange={(on) =>
                patch.mutate(
                  { webapp_enabled: on },
                  {
                    onSuccess: () => toast.success(on ? t("webapp.enabled") : t("webapp.disabled")),
                    onError: (error) => toast.error(errorMessage(error, tc)),
                  },
                )
              }
            />
            <div className="min-w-0">
              <label htmlFor="webapp-enabled" className="text-sm font-medium text-ink">
                {t("webapp.enable")}
              </label>
              <p id="webapp-enabled-desc" className="text-xs text-ink-muted">
                {t("webapp.enableHint")}
              </p>
            </div>
          </div>
          {agent.webapp_enabled ? (
            <div className="flex flex-wrap items-center gap-2">
              <code className="min-w-0 flex-1 rounded-md bg-code px-2.5 py-1.5 font-mono text-sm break-all text-code-ink">
                {url}
              </code>
              <CopyButton value={url} label={t("webapp.copy")} />
              <Button asChild size="sm" variant="secondary">
                <a href={webAppPath(agent.slug)} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="size-3.5" aria-hidden />
                  {t("webapp.open")}
                </a>
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

export function ApiTab({ agent }: { agent: AgentOut }) {
  const { t } = useTranslation("agents");
  return (
    <div className="flex flex-col gap-4">
      {!agent.published_version_id ? <Alert tone="info" title={t("apiKeys.notPublished")} /> : null}
      <WebAppPanel agent={agent} />
      <ApiKeyPanel agent={agent} />
      <Panel title={t("apiExamples.title")} description={t("apiExamples.description")}>
        <ApiExampleList />
      </Panel>
    </div>
  );
}
