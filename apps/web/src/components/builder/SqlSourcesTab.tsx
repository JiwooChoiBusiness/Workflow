import { TriangleAlert, X } from "lucide-react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { AgentWarning } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { Checkbox } from "@/components/ui/toggle";
import { MAX_AGENT_SQL_SOURCES, type AgentForm } from "@/features/agents/schemas";
import { useKnowledgeDefaults } from "@/features/knowledge/api";
import { useSqlSources } from "@/features/sql/api";
import "@/features/sql/i18n";

/*
 * 에이전트 빌더 지식 탭의 데이터 소스 묶음(3단계 설계 10.3, 12.3). 10개까지 고르고, 붙이면
 * describe_sql_source·query_sql 도구가 저절로 생긴다(고르는 도구가 아니다). 유료 키 전용 소스에는 배지를 달고,
 * 저장 경고(source_id가 있는 것)는 그 소스 줄에 붙인다.
 */
export function SqlSourcesTab({
  form,
  warnings,
}: {
  form: UseFormReturn<AgentForm>;
  // 마지막 초안 저장 응답의 경고 중 데이터 소스 것(source_id가 있음)
  warnings: readonly AgentWarning[];
}) {
  const { t } = useTranslation("sql");
  const { t: tk } = useTranslation("knowledge");
  const sources = useSqlSources();
  const defaults = useKnowledgeDefaults();
  const selected = useWatch({ control: form.control, name: "sql_sources" });
  const full = selected.length >= MAX_AGENT_SQL_SOURCES;
  const byId = new Map((sources.data ?? []).map((source) => [source.id, source]));
  const chosen = selected.flatMap((id) => {
    const source = byId.get(id);
    return source ? [source] : [];
  });
  const missing = sources.isSuccess ? selected.filter((id) => !byId.has(id)) : [];
  const paid = chosen.filter((source) => source.key_tier === "paid");
  const notReady = chosen.some((source) => source.state !== "ready");
  const listError = form.formState.errors.sql_sources?.message;

  function setSelected(next: string[]) {
    form.setValue("sql_sources", next, { shouldDirty: true, shouldValidate: true });
  }

  const warningFor = (id: string) =>
    warnings.find((warning) => warning.source_id === id && warning.code !== "paid_key_required")
      ?.message;

  let body;
  if (sources.isPending) body = <Spinner className="size-4" label={t("agent.loading")} />;
  else if (sources.isError) {
    body = (
      <ErrorAlert
        error={sources.error}
        title={t("agent.loadFailed")}
        onRetry={() => void sources.refetch()}
      />
    );
  } else {
    body = (
      <div className="flex flex-col gap-2">
        <p className="text-xs text-ink-muted" aria-live="polite">
          {t("agent.count", { count: selected.length, max: MAX_AGENT_SQL_SOURCES })}
        </p>
        {sources.data.length === 0 ? (
          <p className="rounded-md border border-dashed border-line px-3 py-3 text-center text-xs text-ink-muted">
            {t("agent.empty")}{" "}
            <Link to="/knowledge/sql-sources" className="text-accent hover:underline">
              {t("agent.create")}
            </Link>
          </p>
        ) : null}
        <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
          {sources.data.map((source) => {
            const id = `agent-sql-${source.id}`;
            const checked = selected.includes(source.id);
            const issue = checked ? warningFor(source.id) : undefined;
            return (
              <li key={source.id} className="flex items-start gap-2.5 px-2.5 py-2">
                <Checkbox
                  id={id}
                  className="mt-0.5"
                  checked={checked}
                  disabled={!checked && full}
                  aria-describedby={`${id}-meta`}
                  onCheckedChange={(value) =>
                    setSelected(
                      value === true
                        ? [...selected, source.id]
                        : selected.filter((item) => item !== source.id),
                    )
                  }
                />
                <div className="min-w-0 flex-1">
                  <label
                    htmlFor={id}
                    className="flex flex-wrap items-center gap-1.5 text-sm font-medium text-ink"
                  >
                    <span className="min-w-0 truncate">{source.name}</span>
                    {source.key_tier === "paid" ? (
                      <Badge tone="warn" title={t("policy.tierPaid")}>
                        {tk("tier.paid")}
                      </Badge>
                    ) : null}
                  </label>
                  <p id={`${id}-meta`} className="truncate text-xs text-ink-muted">
                    {t("agent.meta", {
                      kind: t(`kind.${source.kind}`),
                      tables: source.table_count,
                      state: t(`state.${source.state}`),
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
                <p className="text-sm font-medium text-danger">{t("agent.missing")}</p>
                <p className="truncate text-xs text-ink-muted">
                  {warningFor(id) ?? t("agent.missingHint", { id })}
                </p>
              </div>
              <Button
                size="iconSm"
                variant="ghost"
                aria-label={t("agent.remove", { id: id.slice(0, 8) })}
                title={t("agent.remove", { id: id.slice(0, 8) })}
                onClick={() => setSelected(selected.filter((item) => item !== id))}
              >
                <X className="size-4" aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
        {full ? (
          <p className="text-xs text-ink-muted">
            {t("agent.full", { max: MAX_AGENT_SQL_SOURCES })}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <Panel title={t("agent.title")} description={t("agent.description")}>
      <div className="flex flex-col gap-3">
        {body}
        {listError ? (
          <p className="text-xs text-danger" role="alert">
            {listError}
          </p>
        ) : null}
        {selected.length > 0 ? (
          <Alert tone="info" title={t("agent.toolsAttached")}>
            {t("agent.toolsAttachedHint")}
          </Alert>
        ) : null}
        {notReady ? <Alert tone="warn" title={t("agent.notReady")} /> : null}
        {paid.length > 0 ? (
          <Alert tone="warn" title={t("agent.paidTitle")}>
            {defaults.data && !defaults.data.paid_key
              ? t("agent.paidMissing")
              : t("agent.paidHint", { names: paid.map((source) => source.name).join(", ") })}
          </Alert>
        ) : null}
      </div>
    </Panel>
  );
}
