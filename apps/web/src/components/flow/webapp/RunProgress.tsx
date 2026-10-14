import { CircleCheck, CircleDashed, CircleDot, CirclePause, CircleX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Spinner } from "@/components/ui/spinner";
import "@/features/workflows/i18n";
import type { NodeRunStatus, NodeRunView, WfRunViewState } from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import { NodeDuration } from "../run/NodeRunCard";

// 생성 중인 글은 끝부분만 미리 보인다.
const PREVIEW_CHARS = 280;

function StatusIcon({ status }: { status: NodeRunStatus }) {
  const className = "mt-0.5 size-4 shrink-0";
  switch (status) {
    case "running":
      return <Spinner className={cn(className, "text-info")} />;
    case "succeeded":
      return <CircleCheck className={cn(className, "text-ok")} aria-hidden />;
    case "failed":
      return <CircleX className={cn(className, "text-danger")} aria-hidden />;
    case "waiting_input":
      return <CirclePause className={cn(className, "text-warn")} aria-hidden />;
    case "cancelled":
      return <CircleDot className={cn(className, "text-ink-subtle")} aria-hidden />;
    default:
      return <CircleDashed className={cn(className, "text-ink-subtle")} aria-hidden />;
  }
}

function Line({ run, state }: { run: NodeRunView; state: WfRunViewState }) {
  const { t } = useTranslation("workflows");
  const iteration = state.iterations[run.nodeId];
  const index = iteration?.index ?? null;
  const done = iteration?.count ?? (index === null ? 0 : index + 1);
  const preview =
    run.status === "running" && run.text
      ? run.text.length > PREVIEW_CHARS
        ? `…${run.text.slice(-PREVIEW_CHARS)}`
        : run.text
      : "";
  return (
    <li className="flex items-start gap-2 text-sm" data-status={run.status}>
      <StatusIcon status={run.status} />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-ink">{run.title || run.nodeId}</span>
          <span className="text-xs text-ink-subtle">{t(`node.status.${run.status}`)}</span>
          {iteration?.total ? (
            <span className="text-xs text-ink-muted tabular-nums">
              {t("node.iterationProgress", { done, total: iteration.total })}
            </span>
          ) : null}
          <span className="ml-auto text-xs text-ink-subtle tabular-nums">
            <NodeDuration run={run} />
          </span>
        </p>
        {preview ? (
          <p className="mt-1 line-clamp-4 text-xs whitespace-pre-wrap text-ink-muted">{preview}</p>
        ) : null}
        {run.error ? (
          <p className="mt-1 text-xs text-danger">
            <span className="font-mono">{run.error.code}</span> {run.error.message}
          </p>
        ) : null}
      </div>
    </li>
  );
}

// 웹앱의 진행 표시(설계 7.4 RunProgress): 실행된 노드를 한 줄씩, 도는 llm 노드는 생성 중인 글 끝부분을.
export function RunProgress({ state }: { state: WfRunViewState }) {
  const { t } = useTranslation("workflows");
  const runs = state.order
    .map((id) => state.nodeRuns[id])
    .filter(
      (run): run is NodeRunView =>
        run !== undefined && run.iterationId === null && run.status !== "skipped",
    );
  if (runs.length === 0) {
    return <p className="text-sm text-ink-muted">{t("flowApp.waitingStart")}</p>;
  }
  return (
    <ol className="flex flex-col gap-2" aria-label={t("flowApp.progress")}>
      {runs.map((run) => (
        <Line key={run.id} run={run} state={state} />
      ))}
    </ol>
  );
}
