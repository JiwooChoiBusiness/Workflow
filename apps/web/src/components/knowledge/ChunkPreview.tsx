import { useTranslation } from "react-i18next";
import type { DocForm, DocumentPreview, IndexingTechnique } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { formatDateTime, formatNumber } from "@/lib/format";

const KNOWN_WARNINGS = new Set(["text_truncated", "no_text_layer", "separator_overlap"]);

export interface DailyBudget {
  // 오늘 인제스트가 더 받을 수 있는 항목 수. 세지 않는 등급이면 null.
  remaining: number | null;
  // 일일 429나 하루 몫 소진으로 막혀 있으면 풀리는 때.
  blockedUntil: string | null;
}

// 업로드 대화상자의 미리보기(설계 6.4). 예상 소요는 분당 인제스트 몫으로만 나눈 값이라 일일 한도는 따로 알린다.
export function ChunkPreview({
  preview,
  docForm,
  technique,
  rate,
  daily,
}: {
  preview: DocumentPreview;
  docForm: DocForm;
  technique: IndexingTechnique;
  rate: number;
  daily: DailyBudget | null;
}) {
  const { t } = useTranslation("knowledge");
  const hierarchical = docForm === "hierarchical_model";
  const units = hierarchical ? preview.total_child_chunks : preview.total_chunks;
  const embeds = technique === "high_quality";
  const short =
    embeds &&
    daily?.remaining !== null &&
    daily?.remaining !== undefined &&
    units > daily.remaining;
  return (
    <section aria-label={t("upload.preview")} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone="accent">
          {t("preview.summary", { chunks: formatNumber(preview.total_chunks) })}
        </Badge>
        {hierarchical ? (
          <Badge>{t("preview.children", { count: preview.total_child_chunks })}</Badge>
        ) : null}
        <Badge>{t("preview.tokens", { count: preview.tokens })}</Badge>
        <Badge tone="info">
          {embeds
            ? t("preview.minutes", { minutes: preview.estimated_minutes })
            : t("preview.minutesNone")}
        </Badge>
      </div>
      {embeds ? (
        <div className="flex flex-col gap-1 text-xs text-ink-muted">
          <p>{t("preview.minutesNote", { rate })}</p>
          {daily?.blockedUntil ? (
            <p className="text-warn">
              {t("limit.blocked", { time: formatDateTime(daily.blockedUntil) })}
            </p>
          ) : daily ? (
            <p>
              {daily.remaining === null
                ? t("limit.dailyNotCounted")
                : t("limit.dailyRemaining", { count: daily.remaining })}
            </p>
          ) : null}
        </div>
      ) : null}
      {short ? <Alert tone="warn" title={t("limit.dailyShort")} /> : null}
      {preview.warnings.map((code) => (
        <Alert
          key={code}
          tone="warn"
          title={
            KNOWN_WARNINGS.has(code)
              ? t(
                  `preview.warnings.${code as "text_truncated" | "no_text_layer" | "separator_overlap"}`,
                )
              : code
          }
        />
      ))}
      {preview.chunks.length === 0 ? (
        <p className="text-sm text-ink-muted">{t("preview.empty")}</p>
      ) : (
        <>
          {preview.chunks.length < preview.total_chunks ? (
            <p className="text-xs text-ink-muted">
              {t("preview.shown", { count: preview.chunks.length })}
            </p>
          ) : null}
          <ol className="flex max-h-80 flex-col gap-2 overflow-y-auto pr-1">
            {preview.chunks.map((chunk, index) => (
              <li key={index} className="rounded-md border border-line bg-surface px-3 py-2">
                <p className="mb-1 text-xs text-ink-subtle">
                  {t("preview.chunk", { index: index + 1, tokens: chunk.tokens })}
                </p>
                <p className="line-clamp-6 text-sm whitespace-pre-wrap text-ink">{chunk.content}</p>
                {chunk.answer ? (
                  <p className="mt-1 text-sm whitespace-pre-wrap text-ink-muted">
                    <span className="font-medium text-ink">{t("preview.answer")}: </span>
                    {chunk.answer}
                  </p>
                ) : null}
                {chunk.children && chunk.children.length > 0 ? (
                  <ol className="mt-2 flex flex-col gap-1 border-l-2 border-line pl-2">
                    {chunk.children.map((child, childIndex) => (
                      <li key={childIndex} className="text-xs text-ink-muted">
                        <span className="text-ink-subtle">
                          {t("preview.child", { index: childIndex + 1, tokens: child.tokens })}
                        </span>{" "}
                        <span className="line-clamp-2 whitespace-pre-wrap">{child.content}</span>
                      </li>
                    ))}
                  </ol>
                ) : null}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
