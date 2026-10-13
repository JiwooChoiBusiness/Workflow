import { FilterX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Select, type SelectOption } from "@/components/ui/select";
import {
  LOG_PERIODS,
  clearFilters,
  hasActiveFilters,
  type LogFilters,
} from "@/features/logs/filters";
import { RUN_STATUSES, SESSION_SOURCES } from "@/features/logs/types";

// Radix Select는 빈 문자열 값을 받지 않으므로 "전체"를 따로 표시한다.
const ALL = "__all__";

export function LogFilterBar({
  filters,
  agents,
  lockAgent = false,
  onChange,
}: {
  filters: LogFilters;
  agents: { id: string; name: string }[];
  // 에이전트별 로그 화면(/agents/:id/logs)에서는 에이전트를 고를 수 없다.
  lockAgent?: boolean;
  onChange: (next: LogFilters) => void;
}) {
  const { t } = useTranslation("logs");
  const all: SelectOption = { value: ALL, label: t("filters.all") };
  const knownAgent = agents.some((agent) => agent.id === filters.agent);
  const agentOptions: SelectOption[] = [
    all,
    ...agents.map((agent) => ({ value: agent.id, label: agent.name })),
    // 지운 에이전트를 주소로 고른 경우에도 값이 보이게 둔다.
    ...(filters.agent && !knownAgent
      ? [{ value: filters.agent, label: t("filters.unknownAgent") }]
      : []),
  ];
  const fields = [
    !lockAgent && {
      key: "agent" as const,
      value: filters.agent ?? ALL,
      options: agentOptions,
      apply: (value: string | null) => onChange({ ...filters, agent: value }),
    },
    {
      key: "source" as const,
      value: filters.source ?? ALL,
      options: [
        all,
        ...SESSION_SOURCES.map((source) => ({ value: source, label: t(`sources.${source}`) })),
      ],
      apply: (value: string | null) =>
        onChange({ ...filters, source: value as LogFilters["source"] }),
    },
    {
      key: "period" as const,
      value: filters.period,
      options: LOG_PERIODS.map((period) => ({ value: period, label: t(`periods.${period}`) })),
      apply: (value: string | null) =>
        onChange({ ...filters, period: (value ?? "all") as LogFilters["period"] }),
    },
    filters.tab === "runs" && {
      key: "status" as const,
      value: filters.status ?? ALL,
      options: [
        all,
        ...RUN_STATUSES.map((status) => ({ value: status, label: t(`runStatus.${status}`) })),
      ],
      apply: (value: string | null) =>
        onChange({ ...filters, status: value as LogFilters["status"] }),
    },
  ].filter((field) => field !== false);
  const active = hasActiveFilters(filters, { ignoreAgent: lockAgent });

  return (
    <div role="group" aria-label={t("filters.label")} className="flex flex-wrap items-center gap-2">
      {fields.map((field) => (
        <Select
          key={field.key}
          aria-label={t(`filters.${field.key}`)}
          className="w-48"
          value={field.value}
          onValueChange={(value) => field.apply(value === ALL ? null : value)}
          options={field.options}
          renderValue={(option) => (
            <span>
              <span className="text-ink-muted">{t(`filters.${field.key}`)}: </span>
              {option?.label ?? t("filters.all")}
            </span>
          )}
        />
      ))}
      {active ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onChange(clearFilters(filters, { keepAgent: lockAgent }))}
        >
          <FilterX className="size-3.5" aria-hidden />
          {t("filters.clear")}
        </Button>
      ) : null}
    </div>
  );
}
