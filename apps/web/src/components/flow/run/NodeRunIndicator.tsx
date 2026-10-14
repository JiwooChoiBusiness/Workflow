import { Play } from "lucide-react";
import type { MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import "@/features/workflows/i18n";
import { useFlowRunUi, useOptionalWfRun } from "@/features/workflows/wfRunContext";
import type { NodeRunStatus } from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import { NodeDuration } from "./NodeRunCard";

const DOT: Record<NodeRunStatus, string> = {
  running: "bg-info animate-pulse",
  waiting_input: "bg-warn",
  succeeded: "bg-ok",
  failed: "bg-danger",
  skipped: "border border-line-strong bg-transparent",
  cancelled: "bg-ink-subtle",
};

// 캔버스 카드를 누른 것으로 보지 않게 한다(노드 선택·끌기).
function stop(event: MouseEvent) {
  event.stopPropagation();
}

/*
 * 캔버스 노드 카드의 실행 상태(설계 7.3 캔버스 실행 상태): 점·소요 시간. 누르면 실행 패널에서 그 노드 런을
 * 펼친다. 실행 패널이 없는 곳(편집기 밖)에서는 그리지 않는다.
 */
export function NodeRunIndicator({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("workflows");
  const ui = useFlowRunUi();
  const run = useOptionalWfRun((state) => {
    const id = state.latestByNode[nodeId];
    return id ? (state.nodeRuns[id] ?? null) : null;
  });
  const progress = useOptionalWfRun((state) => state.iterations[nodeId] ?? null);
  if (!run || !ui) return null;
  const status = t(`node.status.${run.status}`);
  const total = progress?.total ?? null;
  // 지금 도는 반복이 몇 번째인지(1부터). 끝났으면 센 수.
  const index = progress?.index ?? null;
  const done = progress?.count ?? (index === null ? 0 : index + 1);
  return (
    <button
      type="button"
      className="nodrag nopan inline-flex shrink-0 items-center gap-1 rounded-sm px-1 py-0.5 text-[11px] text-ink-muted tabular-nums hover:bg-hover"
      title={t("node.openRun", { status })}
      aria-label={t("node.openRun", { status })}
      data-run-status={run.status}
      onPointerDown={stop}
      onClick={(event) => {
        stop(event);
        ui.focusNodeRun(run.id);
      }}
    >
      <span className={cn("size-2.5 rounded-full", DOT[run.status])} aria-hidden />
      {total !== null && run.status === "running" ? (
        <span>{t("node.iterationProgress", { done, total })}</span>
      ) : null}
      <NodeDuration run={run} />
    </button>
  );
}

// "이 노드만 실행"(설계 7.3). 카드 위에 올렸거나 고른 카드에서만 보인다.
export function SingleRunButton({ nodeId, type }: { nodeId: string; type: string }) {
  const { t } = useTranslation("workflows");
  const ui = useFlowRunUi();
  if (!ui || type === "start" || type === "iteration_start") return null;
  return (
    <button
      type="button"
      className="nodrag nopan grid size-6 shrink-0 place-items-center rounded-sm text-ink-subtle opacity-0 group-hover:opacity-100 group-[.is-selected]:opacity-100 hover:bg-hover hover:text-ink focus-visible:opacity-100"
      title={t("single.button")}
      aria-label={t("single.buttonFor", { id: nodeId })}
      onPointerDown={stop}
      onClick={(event) => {
        stop(event);
        ui.requestSingleRun(nodeId);
      }}
    >
      <Play className="size-3.5" aria-hidden />
    </button>
  );
}
