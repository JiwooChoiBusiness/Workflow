import { History, Rocket } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import type { AgentOut } from "@/api/types";
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
import type { T } from "@/lib/formSchemas";
import { formatDateTime } from "@/lib/format";

// 발행을 막은 초안 문제(draft_invalid)는 details에 항목별 문구가 있다. "하위 에이전트 X을(를) 먼저 발행한다"처럼
// 무엇을 고칠지가 거기 있으므로 같이 보여 준다. 에이전트는 {loc, msg}, 워크플로우는 검증 이슈 {message}다.
export function publishErrorMessage(error: unknown, tc: T): string {
  if (isApiError(error, 422) && error.code === "draft_invalid" && Array.isArray(error.details)) {
    const details = (error.details as { msg?: unknown; message?: unknown }[])
      .map((detail) =>
        typeof detail.msg === "string"
          ? detail.msg
          : typeof detail.message === "string"
            ? detail.message
            : "",
      )
      .filter(Boolean);
    if (details.length > 0) return [error.message, ...details].join(" ");
  }
  return errorMessage(error, tc);
}

export interface VersionItem {
  id: string;
  version_number: number;
  change_note?: string | null;
  published_at?: string | null;
  is_current: boolean;
}

export interface PublishResult {
  version: { version_number?: number | null };
  warnings?: { message: string }[] | null;
}

// 에이전트와 워크플로우가 같은 발행 화면을 쓴다. 다른 것은 API와 몇몇 안내 문구뿐이다.
export interface PublishSource {
  versions: {
    data: VersionItem[] | undefined;
    isPending: boolean;
    isError: boolean;
    error: unknown;
    refetch: () => unknown;
  };
  publish: {
    isPending: boolean;
    mutate: (
      note: string | null,
      options: { onSuccess: (result: PublishResult) => void; onError: (error: unknown) => void },
    ) => void;
  };
  restore: {
    isPending: boolean;
    mutate: (
      versionId: string,
      options: { onSuccess: () => void; onError: (error: unknown) => void },
    ) => void;
  };
}

export function PublishPanelView({
  source,
  dirty,
  beforePublish,
  description,
  pendingText,
  onPublishError,
}: {
  source: PublishSource;
  dirty: boolean;
  // 있으면 저장하지 않은 변경이 있어도 버튼을 막지 않고 먼저 이것(저장)을 부른다. false면 발행하지 않는다.
  beforePublish?: () => Promise<boolean>;
  description?: string;
  pendingText?: string;
  onPublishError?: (error: unknown) => void;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const { versions, publish, restore } = source;
  const [note, setNote] = useState("");
  const [restoring, setRestoring] = useState<VersionItem | null>(null);
  const [preparing, setPreparing] = useState(false);
  const blockedByDirty = dirty && !beforePublish;

  const published = [...(versions.data ?? [])].sort((a, b) => b.version_number - a.version_number);

  async function submit() {
    if (beforePublish) {
      setPreparing(true);
      const ready = await beforePublish().finally(() => setPreparing(false));
      if (!ready) return;
    }
    publish.mutate(note.trim() || null, {
      onSuccess: ({ version, warnings }) => {
        setNote("");
        toast.success(t("publish.published", { version: version.version_number ?? "?" }));
        if (warnings?.length) {
          toast.warning(
            t("publish.warnings", {
              messages: warnings.map((warning) => warning.message).join(" / "),
            }),
          );
        }
      },
      onError: (error) => {
        onPublishError?.(error);
        toast.error(publishErrorMessage(error, tc));
      },
    });
  }

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
      <Panel title={t("publish.title")} description={description ?? t("publish.description")}>
        <div className="flex flex-col gap-4">
          <Field label={t("publish.changeNote")}>
            <Textarea
              rows={2}
              value={note}
              placeholder={t("publish.changeNotePlaceholder")}
              onChange={(event) => setNote(event.target.value)}
            />
          </Field>
          {blockedByDirty ? <Alert tone="warn" title={t("publish.saveFirst")} /> : null}
          {publish.isPending ? (
            <Alert tone="info" title={pendingText ?? t("publish.pending")} />
          ) : null}
          <div>
            <Button
              variant="primary"
              disabled={blockedByDirty}
              loading={publish.isPending || preparing}
              onClick={() => void submit()}
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

export function PublishPanel({ agent, dirty }: { agent: AgentOut; dirty: boolean }) {
  const versions = useVersions(agent.id);
  const publish = usePublish(agent.id);
  const restore = useRestoreVersion(agent.id);
  return <PublishPanelView source={{ versions, publish, restore }} dirty={dirty} />;
}
