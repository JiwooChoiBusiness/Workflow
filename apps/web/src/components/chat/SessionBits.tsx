import { Download, FileDiff, FoldVertical } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatBytes, formatNumber } from "@/lib/format";
import { useRunStore } from "@/stores/runStoreContext";

export function SessionAutoApproveChip({
  onDisable,
  pending = false,
}: {
  onDisable?: () => void;
  pending?: boolean;
}) {
  const { t } = useTranslation("chat");
  const on = useRunStore((state) => state.sessionAutoApprove);
  if (!on) return null;
  return (
    <span className="inline-flex items-center gap-1" title={t("autoApprove.hint")}>
      <Badge tone="warn">{t("autoApprove.on")}</Badge>
      {onDisable ? (
        <Button size="sm" variant="ghost" loading={pending} onClick={onDisable}>
          {t("autoApprove.off")}
        </Button>
      ) : null}
    </span>
  );
}

// 메시지 기록에서 다시 그린 요약에는 토큰 수가 없다.
export function CompactionDivider({
  before,
  after,
}: {
  before: number | null;
  after: number | null;
}) {
  const { t } = useTranslation("chat");
  return (
    <div className="flex items-center gap-3 py-1 text-xs text-ink-subtle" role="separator">
      <span className="h-px flex-1 bg-line" />
      <FoldVertical className="size-3.5" aria-hidden />
      <span>
        {before !== null && after !== null
          ? t("compaction", { before: formatNumber(before), after: formatNumber(after) })
          : t("compactionNoTokens")}
      </span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

// 런이 바꾼 파일(file.changed와 메시지 기록의 files). 다운로드는 항상 storage 복사본에서 받는다(설계 3.13).
export function FileChangeList({
  fileHref,
}: {
  fileHref?: (sessionFileId: string) => string | null;
}) {
  const { t } = useTranslation("chat");
  const files = useRunStore((state) => state.files);
  const notStoredText = (reason: string | null) =>
    reason === "too_large" || reason === "run_limit" || reason === "copy_failed"
      ? t(`files.reasons.${reason}`)
      : t("files.notStored");
  if (files.length === 0) return null;
  return (
    <section className="border-t border-line bg-surface px-4 py-2.5">
      <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-ink-muted">
        <FileDiff className="size-3.5" aria-hidden />
        {t("files.title")}
      </h3>
      <ul className="flex max-h-32 flex-col gap-1 overflow-y-auto">
        {files.map((file) => {
          const href =
            fileHref && file.downloadable && file.sessionFileId
              ? fileHref(file.sessionFileId)
              : null;
          return (
            <li key={file.path} className="flex items-center gap-2 text-xs">
              <span className="min-w-0 flex-1 truncate font-mono text-ink">{file.path}</span>
              <span className="shrink-0 text-ink-subtle tabular-nums">
                {formatBytes(file.size)}
              </span>
              {href ? (
                <a
                  href={href}
                  download
                  className="inline-flex shrink-0 items-center gap-1 text-accent hover:underline"
                  aria-label={t("files.download", { path: file.path })}
                >
                  <Download className="size-3.5" aria-hidden />
                </a>
              ) : (
                <span className="shrink-0 text-ink-subtle">
                  {file.downloadable ? "" : notStoredText(file.reason)}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
