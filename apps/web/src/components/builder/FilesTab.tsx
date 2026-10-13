import { FileText, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { AgentFileOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { EmptyState, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Checkbox } from "@/components/ui/toggle";
import { useAgentFiles, useDeleteAgentFile, useUploadAgentFile } from "@/features/agents/api";
import type { AgentForm } from "@/features/agents/schemas";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";

// 설계 6.2: 에이전트 첨부 파일은 파일당 100MB까지
const MAX_FILE_BYTES = 100 * 1024 * 1024;

export function FilesTab({ form, agentId }: { form: UseFormReturn<AgentForm>; agentId: string }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const files = useAgentFiles(agentId);
  const upload = useUploadAgentFile(agentId);
  const remove = useDeleteAgentFile(agentId);
  const selected = useWatch({ control: form.control, name: "file_ids" });
  const inputRef = useRef<HTMLInputElement>(null);
  const [deleting, setDeleting] = useState<AgentFileOut | null>(null);

  const setSelected = (ids: string[]) => form.setValue("file_ids", ids, { shouldDirty: true });

  function onPick(file: File | undefined) {
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      toast.error(t("files.tooLarge"));
      return;
    }
    upload.mutate(file, {
      onSuccess: (created) => {
        setSelected([...form.getValues("file_ids"), created.id]);
        toast.success(t("files.uploaded"));
      },
      onError: (error) => toast.error(errorMessage(error, tc)),
    });
  }

  let body;
  if (files.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (files.isError) {
    body = (
      <ErrorAlert
        error={files.error}
        title={t("files.loadFailed")}
        onRetry={() => void files.refetch()}
      />
    );
  } else if (files.data.length === 0) {
    body = <EmptyState icon={FileText} title={t("files.empty")} hint={t("files.maxSize")} />;
  } else {
    body = (
      <ul className="divide-y divide-line">
        {files.data.map((file) => {
          const checked = selected.includes(file.id);
          // 저장된 초안이나 발행 버전이 쓰는 파일은 서버가 지우지 않는다(409 file_in_use).
          const inUse = file.in_draft || file.published_versions.length > 0;
          return (
            <li key={file.id} className="flex items-center gap-3 py-2.5 first:pt-0 last:pb-0">
              <Checkbox
                checked={checked}
                aria-label={t("files.include", { name: file.filename })}
                onCheckedChange={(value) =>
                  setSelected(
                    value === true
                      ? [...selected, file.id]
                      : selected.filter((id) => id !== file.id),
                  )
                }
              />
              <FileText className="size-4 shrink-0 text-ink-subtle" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-ink">{file.filename}</p>
                <p className="flex flex-wrap items-center gap-x-2 text-xs text-ink-muted">
                  <span>
                    {formatBytes(file.size_bytes)} · {file.mime} · {formatDateTime(file.created_at)}
                  </span>
                  {file.in_draft ? <Badge>{t("files.inDraft")}</Badge> : null}
                  {file.published_versions.length > 0 ? (
                    <Badge tone="accent">
                      {t("files.publishedIn", {
                        versions: file.published_versions.map((v) => `v${v}`).join(", "),
                      })}
                    </Badge>
                  ) : null}
                </p>
              </div>
              <Button
                variant="dangerGhost"
                size="iconSm"
                disabled={inUse}
                title={inUse ? t("files.inUse") : undefined}
                aria-label={`${tc("actions.delete")}: ${file.filename}`}
                onClick={() => setDeleting(file)}
              >
                <Trash2 className="size-4" aria-hidden />
              </Button>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <Panel
      title={t("files.title")}
      description={t("files.description")}
      actions={
        <>
          <input
            ref={inputRef}
            type="file"
            className="hidden"
            aria-hidden
            tabIndex={-1}
            onChange={(event) => {
              onPick(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
          <Button size="sm" loading={upload.isPending} onClick={() => inputRef.current?.click()}>
            <Upload className="size-3.5" aria-hidden />
            {t("files.upload")}
          </Button>
        </>
      }
    >
      {form.formState.errors.file_ids?.message ? (
        <Alert tone="danger" className="mb-3" title={form.formState.errors.file_ids.message} />
      ) : null}
      {body}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("files.deleteTitle")}
        description={t("files.deleteBody", { name: deleting?.filename ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          const fileId = deleting.id;
          remove.mutate(fileId, {
            onSuccess: () => {
              setSelected(form.getValues("file_ids").filter((id) => id !== fileId));
              toast.success(t("files.deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
    </Panel>
  );
}
