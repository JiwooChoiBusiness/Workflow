import { ChevronRight, CornerDownRight, RotateCcw } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { LiveElapsed } from "@/components/chat/RunStatusBar";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { JsonTree } from "@/components/common/JsonTree";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { useWfNodeRun } from "@/features/workflows/api";
import "@/features/workflows/i18n";
import { nodeMeta, TONE_CLASS } from "@/features/workflows/nodeTypes";
import {
  isNodeRunFinished,
  type NodeRunStatus,
  type NodeRunView,
} from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import { formatDuration, formatNumber } from "@/lib/format";
import { nodeLabel } from "../labels";

export const NODE_STATUS_TONE: Record<NodeRunStatus, BadgeTone> = {
  running: "info",
  waiting_input: "warn",
  succeeded: "ok",
  failed: "danger",
  skipped: "neutral",
  cancelled: "neutral",
};

// 분기 노드가 고른 핸들(case_<id>, class_<id>, else, fail)을 읽을 수 있게 바꾼다.
function branchLabel(t: TFunction<"workflows">, handle: string): string {
  if (handle === "else") return t("card.else");
  if (handle === "fail") return t("card.fail");
  if (handle.startsWith("case_")) return t("card.case", { n: handle.slice(5) });
  if (handle.startsWith("class_")) return t("node.class", { id: handle.slice(6) });
  return handle;
}

export interface NodeInfo {
  type: string;
  title: string;
  // 그래프에서 이 노드가 든 iteration(바깥이면 null)
  parentId: string | null;
}

export function NodeStatusBadge({ status }: { status: NodeRunStatus }) {
  const { t } = useTranslation("workflows");
  return (
    <Badge tone={NODE_STATUS_TONE[status]}>
      {status === "running" ? <Spinner className="size-3" /> : null}
      {t(`node.status.${status}`)}
    </Badge>
  );
}

export function NodeDuration({ run }: { run: NodeRunView }) {
  if (run.status === "running" && run.startedAt) {
    const start = new Date(run.startedAt).getTime();
    if (!Number.isNaN(start)) return <LiveElapsed start={start} />;
  }
  return run.durationMs !== null && run.status !== "skipped" ? (
    <>{formatDuration(run.durationMs)}</>
  ) : null;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="mb-1 text-xs font-medium text-ink-muted">{title}</h4>
      {children}
    </section>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <div className="max-h-80 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs text-code-ink">
      <JsonTree value={value} openDepth={1} />
    </div>
  );
}

// 펼친 노드 런의 전문(inputs·outputs·process). 이벤트에는 4KB 미리보기만 있어 서버에서 다시 받는다(설계 3.9).
function NodeRunDetail({ runId, run }: { runId: string; run: NodeRunView }) {
  const { t } = useTranslation("workflows");
  const detail = useWfNodeRun(runId, run.id);
  if (detail.isPending) {
    return <Spinner className="size-4" label={t("node.loading")} />;
  }
  if (detail.isError) {
    return (
      <ErrorAlert
        error={detail.error}
        title={t("node.loadFailed")}
        onRetry={() => void detail.refetch()}
      />
    );
  }
  const data = detail.data;
  const process = data.process;
  return (
    <div className="flex flex-col gap-3">
      {data.truncated ? <p className="text-xs text-warn">{t("node.truncated")}</p> : null}
      <Section title={t("node.inputs")}>
        {data.inputs === null || data.inputs === undefined ? (
          <p className="text-xs text-ink-subtle">{t("node.notStored")}</p>
        ) : (
          <JsonBlock value={data.inputs} />
        )}
      </Section>
      <Section title={t("node.outputs")}>
        {data.outputs === null || data.outputs === undefined ? (
          <p className="text-xs text-ink-subtle">{t("node.notStored")}</p>
        ) : (
          <JsonBlock value={data.outputs} />
        )}
      </Section>
      {Object.keys(process).length > 0 ? (
        <Section title={t("node.process")}>
          <JsonBlock value={process} />
        </Section>
      ) : null}
    </div>
  );
}

/*
 * 노드 런 하나(설계 7.3 NodeRunCard): 상태·소요·사용량을 한 줄로 보이고, 펼치면 오류·경고·생성 중인 글과
 * 전문을 보인다. agent 노드의 자식 런은 renderChild가, iteration 안쪽은 children이 그린다.
 */
export function NodeRunCard({
  run,
  runId,
  info,
  focused = false,
  defaultOpen = false,
  fetchDetail = true,
  renderChild,
  children,
}: {
  run: NodeRunView;
  runId: string | null;
  info?: NodeInfo;
  focused?: boolean;
  defaultOpen?: boolean;
  // 전문은 콘솔 API로만 받는다. 웹앱은 미리보기까지만 보인다.
  fetchDetail?: boolean;
  renderChild?: (run: NodeRunView) => ReactNode;
  children?: ReactNode;
}) {
  const { t } = useTranslation("workflows");
  // 사용자가 직접 접거나 펼치기 전에는 입력을 기다리는 노드를 펼쳐 둔다.
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? (defaultOpen || focused || run.status === "waiting_input");
  const header = useRef<HTMLDivElement>(null);
  // 캔버스에서 고른 노드 런으로 한 번 옮긴다. 도는 동안 다시 그려질 때마다 옮기지 않는다.
  useEffect(() => {
    if (focused) header.current?.scrollIntoView({ block: "nearest" });
  }, [focused]);
  const type = run.nodeType ?? info?.type ?? "";
  const meta = nodeMeta(type);
  const Icon = meta.icon;
  const title = run.title || info?.title || nodeLabel(t, type) || run.nodeId;
  const finished = isNodeRunFinished(run.status);
  const skipped = run.status === "skipped";
  const usage = run.usage;
  const child = renderChild && (run.childRunId || run.childSessionId) ? renderChild(run) : null;
  // 자식 런이 도는 동안에는 접어 두어도 자식(과 손자)의 승인 카드가 보여야 한다.
  const liveChild = !finished;
  const expandable = !skipped;

  return (
    <Collapsible.Root
      open={open && expandable}
      onOpenChange={setUserOpen}
      className={cn(
        "rounded-md border bg-surface",
        focused ? "border-accent ring-2 ring-accent/30" : "border-line",
        skipped && "opacity-70",
      )}
      data-testid={`node-run-${run.nodeId}`}
      data-status={run.status}
    >
      <div ref={header}>
        <Collapsible.Trigger
          disabled={!expandable}
          className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left hover:bg-hover disabled:cursor-default disabled:hover:bg-transparent"
        >
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 text-ink-subtle transition-transform",
              open && "rotate-90",
              !expandable && "invisible",
            )}
            aria-hidden
          />
          <span
            className={cn(
              "grid size-6 shrink-0 place-items-center rounded-md",
              TONE_CLASS[meta.tone],
            )}
            title={nodeLabel(t, type)}
          >
            <Icon className="size-3.5" aria-hidden />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-ink">{title}</span>
            <span className="block truncate font-mono text-[11px] text-ink-subtle">
              {run.nodeId}
            </span>
          </span>
          <span className="flex shrink-0 flex-wrap items-center justify-end gap-1.5 text-xs text-ink-subtle tabular-nums">
            {run.attempt > 1 ? <Badge>{t("node.attempt", { n: run.attempt })}</Badge> : null}
            {usage && usage.inputTokens + usage.outputTokens > 0 ? (
              <span title={t("node.tokensHint")}>
                {t("node.tokens", {
                  input: formatNumber(usage.inputTokens),
                  output: formatNumber(usage.outputTokens),
                })}
              </span>
            ) : null}
            <NodeDuration run={run} />
            <NodeStatusBadge status={run.status} />
          </span>
        </Collapsible.Trigger>
      </div>
      {run.waiting || run.retried || run.branch || (run.steps !== null && run.childRunId) ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-9 pb-2 text-xs text-ink-muted">
          {run.waiting ? (
            <span className="text-info">
              {run.waiting.reason === "queue" && run.waiting.position
                ? t("node.queue", { position: run.waiting.position })
                : run.waiting.reason === "queue"
                  ? t("node.queueNoPosition")
                  : t("node.rateLimit")}
            </span>
          ) : null}
          {run.retried ? (
            <span className="inline-flex items-center gap-1 text-warn">
              <RotateCcw className="size-3" aria-hidden />
              {t("node.retried")}
            </span>
          ) : null}
          {run.branch ? (
            <span className="inline-flex items-center gap-1">
              <CornerDownRight className="size-3" aria-hidden />
              {t("node.branch", { branch: branchLabel(t, run.branch) })}
            </span>
          ) : null}
          {run.steps !== null && run.childRunId ? (
            <span>{t("node.steps", { count: run.steps })}</span>
          ) : null}
        </div>
      ) : null}
      {child && liveChild ? <div className="px-3 pb-3">{child}</div> : null}
      <Collapsible.Content className="flex flex-col gap-3 border-t border-line px-3 py-3">
        {run.error ? (
          <div className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-xs">
            <p className="font-mono font-medium text-danger">{run.error.code}</p>
            <p className="mt-0.5 whitespace-pre-wrap text-ink">{run.error.message}</p>
          </div>
        ) : null}
        {run.warnings.length > 0 ? (
          <Section title={t("node.warnings")}>
            <ul className="flex flex-wrap gap-1">
              {run.warnings.map((warning) => (
                <li key={warning}>
                  <Badge tone="warn">{warning}</Badge>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        {run.text && !finished ? (
          <Section title={t("node.livePreview")}>
            <p className="max-h-60 overflow-auto rounded-md bg-sunken px-2.5 py-2 text-xs whitespace-pre-wrap text-ink">
              {run.text}
              {run.textDropped ? (
                <span className="text-ink-subtle"> {t("node.textDropped")}</span>
              ) : null}
            </p>
          </Section>
        ) : null}
        {child && !liveChild ? child : null}
        {children}
        {finished && fetchDetail && runId && open ? (
          <NodeRunDetail runId={runId} run={run} />
        ) : run.outputsPreview && Object.keys(run.outputsPreview).length > 0 ? (
          <Section title={t("node.outputs")}>
            <JsonBlock value={run.outputsPreview} />
            {run.truncated ? (
              <p className="mt-1 text-xs text-warn">{t("node.previewTruncated")}</p>
            ) : null}
          </Section>
        ) : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
