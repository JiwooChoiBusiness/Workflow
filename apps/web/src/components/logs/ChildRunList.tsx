import { ChevronRight } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { sessionLogPath } from "@/features/logs/filters";
import type { RunLogItem } from "@/features/logs/types";
import { formatCost } from "@/features/settings/usage";
import { cn } from "@/lib/cn";
import { formatDuration, formatNumber } from "@/lib/format";
import { RunStatusBadge } from "./badges";
import { RunInspector } from "./RunInspector";

function duration(run: RunLogItem): number | null {
  if (!run.started_at || !run.ended_at) return null;
  return new Date(run.ended_at).getTime() - new Date(run.started_at).getTime();
}

function ChildRunRow({ run }: { run: RunLogItem }) {
  const { t } = useTranslation("logs");
  const [open, setOpen] = useState(false);
  return (
    <li>
      <Collapsible.Root
        open={open}
        onOpenChange={setOpen}
        className="rounded-md border border-line bg-surface"
      >
        <div className="flex items-center gap-2 px-3 py-2">
          <Collapsible.Trigger className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-left text-xs text-ink-muted">
            <ChevronRight
              className={cn(
                "size-3.5 shrink-0 text-ink-subtle transition-transform",
                open && "rotate-90",
              )}
              aria-hidden
            />
            <span className="text-sm font-medium text-ink">{run.agent_name}</span>
            <RunStatusBadge status={run.status} />
            {run.child ? <span>{t("children.progress")}</span> : null}
            <span className="tabular-nums">{t("runs.steps", { count: run.steps })}</span>
            <span className="tabular-nums">
              {t("tokensPair", {
                input: formatNumber(run.usage.input_tokens),
                output: formatNumber(run.usage.output_tokens),
              })}
            </span>
            <span className="tabular-nums">{formatCost(run.usage.cost_usd)}</span>
            <span className="tabular-nums">{formatDuration(duration(run))}</span>
            {run.child_count > 0 ? (
              <Badge>{t("children.count", { count: run.child_count })}</Badge>
            ) : null}
            {run.error ? <span className="font-mono text-danger">{run.error.code}</span> : null}
          </Collapsible.Trigger>
          <Link
            to={sessionLogPath(run.session_id, run.id)}
            className="shrink-0 text-xs text-accent hover:underline"
          >
            {t("children.openSession")}
          </Link>
        </div>
        <Collapsible.Content className="border-t border-line p-3">
          {/* 같은 인스펙터를 안에 다시 연다. 손자가 있으면 그 안에 하위 런 탭이 또 생긴다. */}
          <RunInspector runId={run.id} sessionId={run.session_id} focusToolCallId={null} nested />
        </Collapsible.Content>
      </Collapsible.Root>
    </li>
  );
}

// 런 인스펙터의 하위 런 탭(오케스트레이션 8.3). 부른 순서대로 나열하고, 펼치면 그 자식의 인스펙터가 열린다.
export function ChildRunList({ runs }: { runs: readonly RunLogItem[] }) {
  const { t } = useTranslation("logs");
  if (runs.length === 0) return <p className="text-sm text-ink-muted">{t("children.none")}</p>;
  return (
    <ul aria-label={t("children.label")} className="flex flex-col gap-2">
      {runs.map((run) => (
        <ChildRunRow key={run.id} run={run} />
      ))}
    </ul>
  );
}
