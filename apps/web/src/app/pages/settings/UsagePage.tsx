import { ChartColumn } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState, PageHeader, Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUsage } from "@/features/settings/api";
import {
  USAGE_GROUPS,
  USAGE_PERIODS,
  buildUsageTable,
  type UsageGroupBy,
  type UsagePeriod,
  type UsageTableRow,
} from "@/features/settings/usage";
import { cn } from "@/lib/cn";
import { formatDateTime } from "@/lib/format";

function ShareBar({ share }: { share: number }) {
  return (
    <span className="mt-1 block h-1 w-full overflow-hidden rounded-full bg-sunken" aria-hidden>
      <span
        className="block h-full rounded-full bg-accent/70"
        style={{ width: `${Math.round(share * 100)}%` }}
      />
    </span>
  );
}

function Cells({ row, total = false }: { row: UsageTableRow; total?: boolean }) {
  const { t } = useTranslation("settings");
  const td = cn("px-3 py-2 text-right tabular-nums whitespace-nowrap", total && "font-semibold");
  return (
    <>
      <td className={td}>{row.runs}</td>
      <td className={td}>{row.inputTokens}</td>
      <td className={td}>{row.outputTokens}</td>
      <td className={td}>{row.reasoningTokens}</td>
      <td className={td}>{row.cachedInputTokens}</td>
      <td className={td}>
        {row.cost}
        {row.costPartial ? (
          <span className="ml-1 text-xs font-normal text-warn" title={t("usage.partialHint")}>
            {t("usage.partial")}
          </span>
        ) : null}
      </td>
    </>
  );
}

// 설정 > 사용량(설계 6.2 GET /stats/usage): 일·에이전트·모델별 토큰·비용·런 수.
export default function UsagePage() {
  const { t } = useTranslation("settings");
  const [groupBy, setGroupBy] = useState<UsageGroupBy>("day");
  const [period, setPeriod] = useState<UsagePeriod>("30d");
  const usage = useUsage({ groupBy, period });
  const th = "px-3 py-2 font-medium whitespace-nowrap";
  const table = usage.data ? buildUsageTable(usage.data, t) : null;

  let body;
  if (usage.isPending) {
    body = (
      <div className="flex justify-center p-8">
        <Spinner label={t("usage.loading")} />
      </div>
    );
  } else if (usage.isError) {
    body = (
      <ErrorAlert
        error={usage.error}
        title={t("usage.loadFailed")}
        onRetry={() => void usage.refetch()}
        className="m-4"
      />
    );
  } else if (table === null || table.rows.length === 0) {
    body = <EmptyState icon={ChartColumn} title={t("usage.empty")} className="m-4" />;
  } else {
    body = (
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs text-ink-muted">
            <tr>
              <th className={th}>{t(`usage.column.${groupBy}`)}</th>
              <th className={`${th} text-right`}>{t("usage.column.runs")}</th>
              <th className={`${th} text-right`}>{t("usage.column.input")}</th>
              <th className={`${th} text-right`}>{t("usage.column.output")}</th>
              <th className={`${th} text-right`}>{t("usage.column.reasoning")}</th>
              <th className={`${th} text-right`}>{t("usage.column.cached")}</th>
              <th className={`${th} text-right`}>{t("usage.column.cost")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {table.rows.map((row) => (
              <tr key={row.key}>
                <td className="px-3 py-2">
                  <span
                    className={cn(
                      row.deleted && "text-ink-muted",
                      groupBy === "model" && "font-mono text-xs",
                    )}
                  >
                    {row.label}
                  </span>
                  {row.estimated ? (
                    <Badge className="ml-1.5" title={t("usage.estimatedHint")}>
                      {t("usage.estimated")}
                    </Badge>
                  ) : null}
                  {groupBy !== "day" ? <ShareBar share={row.share} /> : null}
                </td>
                <Cells row={row} />
              </tr>
            ))}
          </tbody>
          <tfoot className="border-t-2 border-line">
            <tr>
              <td className="px-3 py-2 font-semibold">{table.total.label}</td>
              <Cells row={table.total} total />
            </tr>
          </tfoot>
        </table>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title={t("usage.title")} description={t("usage.description")} className="mb-0" />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={groupBy} onValueChange={(value) => setGroupBy(value as UsageGroupBy)}>
          <TabsList aria-label={t("usage.groupLabel")}>
            {USAGE_GROUPS.map((group) => (
              <TabsTrigger key={group} value={group}>
                {t(`usage.group.${group}`)}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <Select
          aria-label={t("usage.periodLabel")}
          className="w-44"
          value={period}
          onValueChange={(value) => setPeriod(value as UsagePeriod)}
          options={USAGE_PERIODS.map((item) => ({ value: item, label: t(`usage.period.${item}`) }))}
        />
      </div>
      {usage.data ? (
        <p className="text-xs text-ink-muted">
          {t("usage.range", {
            since: formatDateTime(usage.data.since),
            until: formatDateTime(usage.data.until),
          })}
        </p>
      ) : null}
      {table && !table.hasCost && table.rows.length > 0 ? (
        <Alert tone="info" title={t("usage.noPrices")}>
          {t("usage.noPricesHint")}{" "}
          <Link to="/settings/workspace" className="text-accent hover:underline">
            {t("usage.pricesLink")}
          </Link>
        </Alert>
      ) : null}
      {table && table.hasCost && table.unpricedRuns > 0 ? (
        <Alert tone="warn" title={t("usage.unpriced", { count: table.unpricedRuns })}>
          {t("usage.unpricedHint")}
        </Alert>
      ) : null}
      <Panel bodyClassName="p-0">{body}</Panel>
      <p className="text-xs text-ink-muted">{t("usage.footnote")}</p>
    </div>
  );
}
