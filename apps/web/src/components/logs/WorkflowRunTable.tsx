import { FilterX, ScrollText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { WfRunOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { Select, type SelectOption } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { LOG_PERIODS } from "@/features/logs/filters";
import { formatPast } from "@/features/logs/time";
import { RUN_STATUSES } from "@/features/logs/types";
import { formatCost } from "@/features/settings/usage";
import "@/features/workflows/i18n";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  shortRunId,
  WF_TRIGGERS,
  wfRunDuration,
  wfRunPath,
  type WfRunFilters,
  type WfTrigger,
} from "@/features/workflows/runLogFilters";
import { formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { RunStatusBadge } from "./badges";

const ALL = "__all__";

const TRIGGER_TONE: Record<WfTrigger, BadgeTone> = {
  preview: "neutral",
  webapp: "accent",
  api: "info",
  single_node: "warn",
  resume: "neutral",
};

export function TriggerBadge({ trigger }: { trigger: WfTrigger }) {
  const { t } = useTranslation("workflows");
  return <Badge tone={TRIGGER_TONE[trigger]}>{t(`trigger.${trigger}`)}</Badge>;
}

export function WorkflowRunFilterBar({
  filters,
  workflows,
  onChange,
}: {
  filters: WfRunFilters;
  // 주면 워크플로우를 고르는 칸을 둔다(/logs).
  workflows?: { id: string; name: string }[];
  onChange: (next: WfRunFilters) => void;
}) {
  const { t } = useTranslation("logs");
  const { t: tw } = useTranslation("workflows");
  const all: SelectOption = { value: ALL, label: t("filters.all") };
  const fields = [
    workflows && {
      key: "workflow",
      label: tw("logs.workflow"),
      value: filters.workflow ?? ALL,
      options: [
        all,
        ...workflows.map((item) => ({ value: item.id, label: item.name })),
        ...(filters.workflow && !workflows.some((item) => item.id === filters.workflow)
          ? [{ value: filters.workflow, label: tw("logs.unknownWorkflow") }]
          : []),
      ],
      apply: (value: string | null) => onChange({ ...filters, workflow: value }),
    },
    {
      key: "trigger",
      label: tw("logs.trigger"),
      value: filters.trigger ?? ALL,
      options: [
        all,
        ...WF_TRIGGERS.map((trigger) => ({ value: trigger, label: tw(`trigger.${trigger}`) })),
      ],
      apply: (value: string | null) => onChange({ ...filters, trigger: value as WfTrigger | null }),
    },
    {
      key: "status",
      label: t("filters.status"),
      value: filters.status ?? ALL,
      options: [
        all,
        ...RUN_STATUSES.map((status) => ({ value: status, label: t(`runStatus.${status}`) })),
      ],
      apply: (value: string | null) =>
        onChange({ ...filters, status: value as WfRunFilters["status"] }),
    },
    {
      key: "period",
      label: t("filters.period"),
      value: filters.period,
      options: LOG_PERIODS.map((period) => ({ value: period, label: t(`periods.${period}`) })),
      apply: (value: string | null) =>
        onChange({ ...filters, period: (value ?? "all") as WfRunFilters["period"] }),
    },
  ].filter((field) => field !== undefined);
  const active = hasWfRunFilters(filters, { ignoreWorkflow: !workflows });

  return (
    <div role="group" aria-label={t("filters.label")} className="flex flex-wrap items-center gap-2">
      {fields.map((field) => (
        <Select
          key={field.key}
          aria-label={field.label}
          className="w-48"
          value={field.value}
          onValueChange={(value) => field.apply(value === ALL ? null : value)}
          options={field.options}
          renderValue={(option) => (
            <span>
              <span className="text-ink-muted">{field.label}: </span>
              {option?.label ?? t("filters.all")}
            </span>
          )}
        />
      ))}
      {active ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            onChange({
              ...DEFAULT_WF_RUN_FILTERS,
              workflow: workflows ? null : filters.workflow,
            })
          }
        >
          <FilterX className="size-3.5" aria-hidden />
          {t("filters.clear")}
        </Button>
      ) : null}
    </div>
  );
}

const th = "px-3 py-2 font-medium whitespace-nowrap";
const td = "px-3 py-2 align-top";

/*
 * 워크플로우 런 목록(설계 7.2): 시작·상태·실행 방식·노드 수·토큰·비용·걸린 시간. 행을 누르면 런 상세로
 * 간다. workflowNames를 주면 워크플로우 칸을 둔다(/logs).
 */
export function WorkflowRunTable({
  runs,
  loading,
  error,
  onRetry,
  hasMore,
  loadingMore,
  onMore,
  filtered,
  onClearFilters,
  workflowNames,
}: {
  runs: readonly WfRunOut[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onMore: () => void;
  filtered: boolean;
  onClearFilters: () => void;
  workflowNames?: Map<string, string>;
}) {
  const { t } = useTranslation("logs");
  const { t: tw } = useTranslation("workflows");
  const now = useNow();
  if (loading) {
    return (
      <div className="flex justify-center p-8">
        <Spinner label={t("list.loading")} />
      </div>
    );
  }
  if (error) {
    return (
      <ErrorAlert error={error} title={t("list.loadFailed")} onRetry={onRetry} className="m-4" />
    );
  }
  if (runs.length === 0) {
    return (
      <EmptyState
        icon={ScrollText}
        title={filtered ? t("list.emptyFiltered") : tw("logs.empty")}
        hint={filtered ? undefined : tw("logs.emptyHint")}
        action={
          filtered ? (
            <Button size="sm" onClick={onClearFilters}>
              {t("filters.clear")}
            </Button>
          ) : null
        }
        className="m-4"
      />
    );
  }
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs text-ink-muted">
            <tr>
              <th className={th}>{t("columns.queued")}</th>
              {workflowNames ? <th className={th}>{tw("logs.workflow")}</th> : null}
              <th className={th}>{tw("logs.run")}</th>
              <th className={th}>{tw("logs.trigger")}</th>
              <th className={th}>{t("columns.status")}</th>
              <th className={`${th} text-right`}>{tw("logs.nodes")}</th>
              <th className={`${th} text-right`}>{t("columns.tokens")}</th>
              <th className={`${th} text-right`}>{t("columns.cost")}</th>
              <th className={`${th} text-right`}>{t("columns.duration")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {runs.map((run) => (
              <tr key={run.id} className="hover:bg-hover/60">
                <td className={`${td} text-ink-muted`}>
                  <time
                    dateTime={run.queued_at}
                    title={formatDateTime(run.queued_at)}
                    className="whitespace-nowrap"
                  >
                    {formatPast(run.queued_at, now)}
                  </time>
                </td>
                {workflowNames ? (
                  <td className={td}>
                    {workflowNames.get(run.workflow_id) ?? (
                      <span className="text-ink-subtle">{tw("logs.unknownWorkflow")}</span>
                    )}
                  </td>
                ) : null}
                <td className={td}>
                  <Link
                    to={wfRunPath(run.workflow_id, run.id)}
                    className="font-mono text-xs font-medium text-ink hover:text-accent hover:underline"
                  >
                    {shortRunId(run.id)}
                  </Link>
                </td>
                <td className={td}>
                  <TriggerBadge trigger={run.trigger} />
                </td>
                <td className={td}>
                  <RunStatusBadge status={run.status} />
                  {run.error ? (
                    <span className="mt-0.5 block font-mono text-xs text-danger">
                      {run.error.code}
                    </span>
                  ) : null}
                </td>
                <td className={`${td} text-right tabular-nums`}>
                  {formatNumber(run.node_run_count)}
                </td>
                <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
                  {t("tokensPair", {
                    input: formatNumber(run.usage.input_tokens),
                    output: formatNumber(run.usage.output_tokens),
                  })}
                </td>
                <td className={`${td} text-right tabular-nums`}>
                  {formatCost(run.usage.cost_usd)}
                </td>
                <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
                  {formatDuration(wfRunDuration(run))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMore ? (
        <div className="flex justify-center border-t border-line py-2">
          <Button size="sm" variant="ghost" loading={loadingMore} onClick={onMore}>
            {t("list.more")}
          </Button>
        </div>
      ) : null}
    </>
  );
}
