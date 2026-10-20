import { ChevronRight } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { WfNodeRunOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import "@/features/workflows/i18n";
import type { IterationView, NodeRunView, WfRunViewState } from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import { NodeRunCard, NodeStatusBadge, type NodeInfo } from "./NodeRunCard";

type Groups = Map<number, NodeRunView[]>;

function groupStatus(runs: NodeRunView[]): NodeRunView["status"] {
  if (runs.some((run) => run.status === "failed")) return "failed";
  if (runs.some((run) => run.status === "waiting_input")) return "waiting_input";
  if (runs.some((run) => run.status === "running")) return "running";
  if (runs.some((run) => run.status === "cancelled")) return "cancelled";
  return "succeeded";
}

interface CardProps {
  runId: string | null;
  nodeInfo: (nodeId: string) => NodeInfo | undefined;
  focusId: string | null;
  fetchDetail: boolean;
  renderChild?: (run: NodeRunView) => ReactNode;
}

function IterationGroup({
  index,
  runs,
  card,
  loop,
}: {
  index: number;
  runs: NodeRunView[];
  card: CardProps;
  loop: boolean;
}) {
  const { t } = useTranslation("workflows");
  const containsFocus = runs.some((run) => run.id === card.focusId);
  // 승인·답을 기다리는 반복은 펼쳐 둔다. 승인 카드가 그 안의 agent 노드 카드에 뜬다.
  const waiting = runs.some((run) => run.status === "waiting_input");
  const [open, setOpen] = useState<boolean | null>(null);
  const shown = open ?? (containsFocus || waiting);
  return (
    <Collapsible.Root open={shown} onOpenChange={setOpen} className="rounded-md border border-line">
      <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs hover:bg-hover">
        <ChevronRight
          className={cn("size-3.5 text-ink-subtle transition-transform", shown && "rotate-90")}
          aria-hidden
        />
        <span className="font-medium text-ink">
          {loop ? t("node.loopRound", { n: index + 1 }) : t("node.iteration", { n: index + 1 })}
        </span>
        <span className="text-ink-subtle">{t("node.iterationNodes", { count: runs.length })}</span>
        <span className="ml-auto">
          <NodeStatusBadge status={groupStatus(runs)} />
        </span>
      </Collapsible.Trigger>
      <Collapsible.Content className="flex flex-col gap-1.5 border-t border-line p-2">
        {runs.map((run) => (
          <NodeRunCard
            key={`${run.id}:${run.id === card.focusId ? "focus" : ""}`}
            run={run}
            runId={card.runId}
            info={card.nodeInfo(run.nodeId)}
            focused={run.id === card.focusId}
            fetchDetail={card.fetchDetail}
            renderChild={card.renderChild}
          />
        ))}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function IterationBody({
  view,
  groups,
  card,
  loadIteration,
  iterationNodeId,
  loop,
}: {
  view: IterationView | undefined;
  groups: Groups | undefined;
  card: CardProps;
  loadIteration?: (iterationNodeId: string) => Promise<WfNodeRunOut[]>;
  iterationNodeId: string;
  // loop는 total이 최대 반복 수라 그 전에 끝날 수 있다.
  loop: boolean;
}) {
  const { t } = useTranslation("workflows");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const indexes = [...(groups?.keys() ?? [])].sort((a, b) => a - b);
  const total = view?.total ?? null;
  const done =
    view?.count ?? (view?.index !== null && view?.index !== undefined ? view.index : null);

  async function load() {
    if (!loadIteration) return;
    setLoading(true);
    setError(null);
    try {
      await loadIteration(iterationNodeId);
    } catch (failure) {
      setError(failure);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <p className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        {total !== null ? (
          <span>
            {loop
              ? t("node.loopProgress", { done: done ?? 0, total })
              : t("node.iterationProgress", { done: done ?? 0, total })}
          </span>
        ) : null}
        {view?.failedCount ? (
          <Badge tone="danger">{t("node.iterationFailed", { count: view.failedCount })}</Badge>
        ) : null}
      </p>
      {indexes.length === 0 ? (
        loadIteration ? (
          <div className="flex items-center gap-2 text-xs text-ink-muted">
            {loading ? <Spinner className="size-3.5" /> : null}
            <button
              type="button"
              className="text-accent hover:underline"
              disabled={loading}
              onClick={() => void load()}
            >
              {t("node.iterationLoad")}
            </button>
          </div>
        ) : (
          <p className="text-xs text-ink-subtle">{t("node.iterationEmpty")}</p>
        )
      ) : (
        indexes.map((index) => (
          <IterationGroup
            key={index}
            index={index}
            runs={groups!.get(index)!}
            card={card}
            loop={loop}
          />
        ))
      )}
      {error ? <ErrorAlert error={error} title={t("node.loadFailed")} /> : null}
    </div>
  );
}

const CONTAINERS: ReadonlySet<string> = new Set(["iteration", "loop"]);

/*
 * 노드 런을 실행 순서대로(설계 7.3 NodeTimeline). iteration·loop 안쪽 노드 런은 그 카드 안에 반복 순번별로
 * 접어 둔다(loop도 iteration.* 이벤트를 내고 안쪽 노드 런의 iteration_id가 loop다). focusId인 노드 런은
 * 펼치고 화면에 보이게 한다.
 */
export function NodeTimeline({
  state,
  nodeInfo,
  focusId = null,
  fetchDetail = true,
  renderChild,
  loadIteration,
}: {
  state: WfRunViewState;
  nodeInfo: (nodeId: string) => NodeInfo | undefined;
  focusId?: string | null;
  fetchDetail?: boolean;
  renderChild?: (run: NodeRunView) => ReactNode;
  loadIteration?: (iterationNodeId: string) => Promise<WfNodeRunOut[]>;
}) {
  const { t } = useTranslation("workflows");
  const { roots, inner } = useMemo(() => {
    const rootRuns: NodeRunView[] = [];
    const byIteration = new Map<string, Groups>();
    for (const id of state.order) {
      const run = state.nodeRuns[id];
      if (!run) continue;
      // 건너뛴 노드의 프레임은 이벤트 순서로 짐작한 것이라(wfRunStore.lastFrame), 병렬 가지에서 어긋날 수
      // 있다. 그래프가 바깥 노드라고 하면 바깥에 둔다.
      if (
        run.iterationId === null ||
        (run.status === "skipped" && nodeInfo(run.nodeId)?.parentId === null)
      ) {
        rootRuns.push(run);
        continue;
      }
      const groups = byIteration.get(run.iterationId) ?? new Map<number, NodeRunView[]>();
      const index = run.iterationIndex ?? -1;
      groups.set(index, [...(groups.get(index) ?? []), run]);
      byIteration.set(run.iterationId, groups);
    }
    return { roots: rootRuns, inner: byIteration };
  }, [state.order, state.nodeRuns, nodeInfo]);

  const card: CardProps = { runId: state.runId, nodeInfo, focusId, fetchDetail, renderChild };
  const anyInside = (iterationNodeId: string, test: (run: NodeRunView) => boolean) =>
    [...(inner.get(iterationNodeId)?.values() ?? [])].some((runs) => runs.some(test));

  if (roots.length === 0) {
    return <p className="text-sm text-ink-muted">{t("run.noNodes")}</p>;
  }
  return (
    <ol className="flex flex-col gap-1.5" aria-label={t("run.timeline")}>
      {roots.map((run) => {
        const type = run.nodeType ?? nodeInfo(run.nodeId)?.type;
        const iteration = CONTAINERS.has(type ?? "");
        const focused = run.id === focusId;
        const containsFocus =
          iteration && focusId !== null && anyInside(run.nodeId, (item) => item.id === focusId);
        const waitingInside =
          iteration && anyInside(run.nodeId, (item) => item.status === "waiting_input");
        return (
          <li key={`${run.id}:${focused || containsFocus ? "focus" : ""}`}>
            <NodeRunCard
              run={run}
              runId={state.runId}
              info={nodeInfo(run.nodeId)}
              focused={focused}
              defaultOpen={containsFocus || waitingInside}
              fetchDetail={fetchDetail}
              renderChild={renderChild}
            >
              {iteration ? (
                <IterationBody
                  view={state.iterations[run.nodeId]}
                  groups={inner.get(run.nodeId)}
                  card={card}
                  loadIteration={loadIteration}
                  iterationNodeId={run.nodeId}
                  loop={type === "loop"}
                />
              ) : null}
            </NodeRunCard>
          </li>
        );
      })}
    </ol>
  );
}
