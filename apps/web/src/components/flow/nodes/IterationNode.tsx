import { Handle, NodeResizer, Position, type Node, type NodeProps } from "@xyflow/react";
import { memo } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import { IN_HANDLE, OUT_HANDLE, type FlowNode } from "@/features/workflows/graph";
import { nodeMeta, TONE_CLASS } from "@/features/workflows/nodeTypes";
import { cn } from "@/lib/cn";
import { IssueDot, useImplemented } from "../NodeCard";
import { nodeLabel } from "../labels";
import { NodeRunIndicator, SingleRunButton } from "../run/NodeRunIndicator";

export type IterationRfNode = Node<FlowNode, "iteration">;

export const ITERATION_MIN = { width: 320, height: 180 };

// iteration은 하위 그래프를 안에 담는 틀이다(설계 7.3). 안쪽 노드는 ReactFlow 하위 흐름(parentId)으로 그린다.
export const IterationNode = memo(function IterationNode({
  id,
  data: node,
  selected,
}: NodeProps<IterationRfNode>) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const counts = useFlow((state) => state.issueCounts.get(id));
  const childCount = useFlow(
    (state) => state.graph.nodes.filter((item) => item.parent_id === id).length,
  );
  const implemented = useImplemented(node.type);
  const meta = nodeMeta(node.type);
  const Icon = meta.icon;
  const label = nodeLabel(t, node.type);
  const input = typeof node.config.input_var === "string" ? node.config.input_var : "";
  const parallel = node.config.parallel === true;
  return (
    <div
      className={cn(
        "group relative h-full w-full rounded-lg border-2 border-dashed bg-surface/70",
        selected && "is-selected",
        selected ? "border-accent" : counts?.errors ? "border-danger" : "border-line-strong",
      )}
      data-testid={`flow-node-${id}`}
    >
      <NodeResizer
        isVisible={selected}
        minWidth={ITERATION_MIN.width}
        minHeight={ITERATION_MIN.height}
        onResizeStart={actions.beginMove}
        onResizeEnd={actions.endMove}
      />
      <Handle type="target" position={Position.Left} id={IN_HANDLE} />
      <div className="flex items-center gap-2 border-b border-dashed border-line px-3 py-2">
        <span
          className={cn(
            "grid size-6 shrink-0 place-items-center rounded-md",
            TONE_CLASS[meta.tone],
          )}
          title={label}
        >
          <Icon className="size-3.5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm leading-tight font-medium text-ink">
            {node.title || label}
          </p>
          <p className="truncate text-xs leading-tight text-ink-muted">
            <span className="font-mono">{id}</span>
            {" · "}
            {t("card.iterate", { input: input || "…" })}
          </p>
        </div>
        {parallel ? (
          <Badge tone="info">
            {t("card.parallel", { count: Number(node.config.max_parallel ?? 1) })}
          </Badge>
        ) : null}
        {!implemented ? <Badge tone="warn">{t("card.notImplemented")}</Badge> : null}
        <SingleRunButton nodeId={id} type={node.type} />
        <NodeRunIndicator nodeId={id} />
        <IssueDot counts={counts} />
      </div>
      {childCount <= 1 ? (
        <p className="pointer-events-none absolute right-3 bottom-2 left-3 text-center text-xs text-ink-subtle">
          {t("canvas.iterationEmpty")}
        </p>
      ) : null}
      <Handle type="source" position={Position.Right} id={OUT_HANDLE} />
    </div>
  );
});
