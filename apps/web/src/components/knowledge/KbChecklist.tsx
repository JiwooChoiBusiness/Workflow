import { TriangleAlert, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { KnowledgeBaseOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Checkbox } from "@/components/ui/toggle";
import "@/features/knowledge/i18n";
import { retrievalSummary } from "@/features/knowledge/messages";

/*
 * 지식 베이스 체크 목록(knowledge_retrieval 노드, 에이전트 빌더 지식 탭). max개까지 고르고, 유료 키 전용
 * KB에는 배지를 단다. 고른 id가 목록에 없으면(지웠거나 다른 PC에서 가져옴) 따로 보이고 뺄 수 있게 한다.
 */
export function KbChecklist({
  kbs,
  loading,
  error,
  onRetry,
  selected,
  onChange,
  max,
  issueFor,
  idPrefix,
}: {
  kbs: readonly KnowledgeBaseOut[] | undefined;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  selected: readonly string[];
  onChange: (next: string[]) => void;
  max: number;
  // 고른 KB의 서버 이슈(지운 KB, 유료 키 등). 고른 순서의 순번도 넘긴다.
  issueFor?: (kbId: string, index: number) => string | undefined;
  idPrefix: string;
}) {
  const { t } = useTranslation("knowledge");
  const full = selected.length >= max;
  const known = new Set((kbs ?? []).map((kb) => kb.id));
  const missing = selected.filter((id) => !known.has(id));

  function toggle(id: string, on: boolean) {
    onChange(on ? [...selected, id] : selected.filter((item) => item !== id));
  }

  if (loading) return <Spinner className="size-4" label={t("pick.loading")} />;
  if (error) return <ErrorAlert error={error} title={t("list.loadFailed")} onRetry={onRetry} />;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-ink-muted" aria-live="polite">
        {t("pick.count", { count: selected.length, max })}
      </p>
      {kbs && kbs.length === 0 ? (
        <p className="rounded-md border border-dashed border-line px-3 py-3 text-center text-xs text-ink-muted">
          {t("pick.empty")}{" "}
          <Link to="/knowledge" className="text-accent hover:underline">
            {t("pick.create")}
          </Link>
        </p>
      ) : null}
      <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
        {(kbs ?? []).map((kb) => {
          const id = `${idPrefix}-${kb.id}`;
          const checked = selected.includes(kb.id);
          const index = selected.indexOf(kb.id);
          const issue = checked ? issueFor?.(kb.id, index) : undefined;
          const paid = kb.data_policy.key_tier === "paid";
          return (
            <li key={kb.id} className="flex items-start gap-2.5 px-2.5 py-2">
              <Checkbox
                id={id}
                className="mt-0.5"
                checked={checked}
                disabled={!checked && full}
                onCheckedChange={(value) => toggle(kb.id, value === true)}
                aria-describedby={`${id}-meta`}
              />
              <div className="min-w-0 flex-1">
                <label
                  htmlFor={id}
                  className="flex flex-wrap items-center gap-1.5 text-sm font-medium text-ink"
                >
                  <span className="min-w-0 truncate">{kb.name}</span>
                  {paid ? (
                    <Badge tone="warn" title={t("tierHint.paid")}>
                      {t("tier.paid")}
                    </Badge>
                  ) : null}
                </label>
                <p id={`${id}-meta`} className="truncate text-xs text-ink-muted">
                  {t("pick.meta", {
                    documents: kb.document_count,
                    technique: t(`technique.${kb.indexing_technique}`),
                    retrieval: retrievalSummary(kb.retrieval_model, t),
                  })}
                </p>
                {issue ? <p className="mt-0.5 text-xs text-warn">{issue}</p> : null}
              </div>
            </li>
          );
        })}
        {missing.map((id) => (
          <li key={id} className="flex items-start gap-2.5 px-2.5 py-2">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-danger">{t("pick.missing")}</p>
              <p className="truncate text-xs text-ink-muted">
                {issueFor?.(id, selected.indexOf(id)) ?? t("pick.missingHint", { id })}
              </p>
            </div>
            <Button
              size="iconSm"
              variant="ghost"
              aria-label={t("pick.remove", { id: id.slice(0, 8) })}
              title={t("pick.remove", { id: id.slice(0, 8) })}
              onClick={() => toggle(id, false)}
            >
              <X className="size-4" aria-hidden />
            </Button>
          </li>
        ))}
      </ul>
      {full ? <p className="text-xs text-ink-muted">{t("pick.full", { max })}</p> : null}
    </div>
  );
}
