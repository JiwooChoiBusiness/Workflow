import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { WfIssue } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useSqlSources } from "@/features/sql/api";
import "@/features/sql/i18n";
import { useKbOptions } from "@/features/workflows/api";

/*
 * SQL 노드의 데이터 소스 고르기. 고른 id가 목록에 없으면(지웠거나 다른 PC에서 가져온 노드) 그 id를 따로 보이고,
 * 준비되지 않은 소스와 유료 키 전용 소스는 칸 아래에 알린다. 서버 이슈 중 오류(sql_source_required·
 * sql_source_not_found)는 칸 오류로, 경고 paid_key_required는 안내로 보인다.
 */
export function SqlSourceField({
  value,
  onChange,
  error,
  issues,
}: {
  value: string;
  onChange: (id: string) => void;
  error?: string;
  issues: readonly WfIssue[];
}) {
  const { t } = useTranslation("sql");
  const sources = useSqlSources();
  const kbOptions = useKbOptions().data;
  const chosen = sources.data?.find((source) => source.id === value);
  const fieldIssue = issues.find(
    (issue) => issue.field === "source_id" && issue.level === "error",
  )?.message;
  const paidIssue = issues.find((issue) => issue.code === "paid_key_required");
  const missing = Boolean(value) && sources.isSuccess && !chosen;
  const options = (sources.data ?? []).map((source) => ({
    value: source.id,
    label: source.name,
    hint: t("node.sourceMeta", {
      kind: t(`kind.${source.kind}`),
      tables: source.table_count,
      state: t(`state.${source.state}`),
    }),
  }));
  if (missing) {
    options.push({ value, label: t("node.missingSource", { id: value.slice(0, 8) }), hint: "" });
  }

  return (
    <Field label={t("node.source")} error={error ?? fieldIssue} required>
      {sources.isPending ? (
        <Spinner className="size-4" label={t("node.source")} />
      ) : sources.isError ? (
        <ErrorAlert
          error={sources.error}
          title={t("list.loadFailed")}
          onRetry={() => void sources.refetch()}
        />
      ) : (
        <div className="flex flex-col gap-2">
          {sources.data.length === 0 ? (
            <p className="text-xs text-ink-muted">
              {t("node.noSources")}{" "}
              <Link to="/knowledge/sql-sources" className="text-accent hover:underline">
                {t("node.createSource")}
              </Link>
            </p>
          ) : (
            <Select
              value={value || undefined}
              placeholder={t("node.pickSource")}
              onValueChange={onChange}
              options={options}
            />
          )}
          {chosen ? (
            <Link
              to={`/knowledge/sql-sources/${chosen.id}`}
              className="inline-flex items-center gap-1 self-start text-xs text-accent hover:underline"
            >
              <ExternalLink className="size-3" aria-hidden />
              {t("node.openSource")}
            </Link>
          ) : null}
          {chosen && chosen.state !== "ready" ? (
            <p className="text-xs text-warn">{t("node.notReady")}</p>
          ) : null}
          {chosen?.key_tier === "paid" || paidIssue ? (
            <Alert tone="warn" title={t("node.paidTitle")} className="text-xs">
              {kbOptions && !kbOptions.paid_key_configured
                ? t("node.paidMissing")
                : (paidIssue?.message ?? t("node.paidHint"))}
            </Alert>
          ) : null}
        </div>
      )}
    </Field>
  );
}
