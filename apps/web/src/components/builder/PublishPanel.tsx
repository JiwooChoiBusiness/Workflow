import { History, Rocket } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { AgentOut, AgentVersionSummary } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { usePublish, useRestoreVersion, useVersions } from "@/features/agents/api";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

export function PublishPanel({ agent, dirty }: { agent: AgentOut; dirty: boolean }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const versions = useVersions(agent.id);
  const publish = usePublish(agent.id);
  const restore = useRestoreVersion(agent.id);
  const [note, setNote] = useState("");
  const [restoring, setRestoring] = useState<AgentVersionSummary | null>(null);

  const published = [...(versions.data ?? [])].sort((a, b) => b.version_number - a.version_number);

  let history;
  if (versions.isPending) {
    history = <PageSpinner label={tc("state.loading")} />;
  } else if (versions.isError) {
    history = (
      <ErrorAlert
        error={versions.error}
        title={t("publish.loadFailed")}
        onRetry={() => void versions.refetch()}
      />
    );
  } else if (published.length === 0) {
    history = <EmptyState icon={History} title={t("publish.empty")} />;
  } else {
    history = (
      <ol className="divide-y divide-line">
        {published.map((version) => {
          const current = version.is_current;
          return (
            <li key={version.id} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
              <Badge tone={current ? "accent" : "neutral"}>
                {t("publish.version", { version: version.version_number })}
              </Badge>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-ink">{version.change_note || t("publish.noNote")}</p>
                <p className="text-xs text-ink-muted">{formatDateTime(version.published_at)}</p>
              </div>
              {current ? (
                <Badge tone="ok">{t("publish.current")}</Badge>
              ) : (
                <Button size="sm" onClick={() => setRestoring(version)}>
                  {t("publish.restore")}
                </Button>
              )}
            </li>
          );
        })}
      </ol>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Panel title={t("publish.title")} description={t("publish.description")}>
        <div className="flex flex-col gap-4">
          <Field label={t("publish.changeNote")}>
            <Textarea
              rows={2}
              value={note}
              placeholder={t("publish.changeNotePlaceholder")}
              onChange={(event) => setNote(event.target.value)}
            />
          </Field>
          {dirty ? <Alert tone="warn" title={t("publish.saveFirst")} /> : null}
          {publish.isPending ? <Alert tone="info" title={t("publish.pending")} /> : null}
          <div>
            <Button
              variant="primary"
              disabled={dirty}
              loading={publish.isPending}
              onClick={() =>
                publish.mutate(note.trim() || null, {
                  onSuccess: ({ version }) => {
                    setNote("");
                    toast.success(
                      t("publish.published", { version: version.version_number ?? "?" }),
                    );
                  },
                  onError: (error) => toast.error(errorMessage(error, tc)),
                })
              }
            >
              <Rocket className="size-4" aria-hidden />
              {t("publish.submit")}
            </Button>
          </div>
        </div>
      </Panel>
      <Panel title={t("publish.history")}>{history}</Panel>
      <ConfirmDialog
        open={restoring !== null}
        onOpenChange={(open) => {
          if (!open) setRestoring(null);
        }}
        tone="default"
        title={t("publish.restoreTitle")}
        description={t("publish.restoreBody", { version: restoring?.version_number ?? "?" })}
        confirmLabel={t("publish.restore")}
        pending={restore.isPending}
        onConfirm={() => {
          if (!restoring) return;
          const version = restoring.version_number;
          restore.mutate(restoring.id, {
            onSuccess: () => {
              toast.success(t("publish.restored", { version }));
              setRestoring(null);
            },
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
    </div>
  );
}
