import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { memo } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useNodeTypes } from "@/features/workflows/api";
import { isImportedCode } from "@/features/workflows/dsl";
import { useFlow } from "@/features/workflows/flowStoreContext";
import {
  ANSWER_TYPE,
  FAIL_HANDLE,
  hasTargetHandle,
  IN_HANDLE,
  OUT_HANDLE,
  prestreamSources,
  type FlowNode,
  type IssueCount,
} from "@/features/workflows/graph";
import { nodeMeta, TONE_CLASS } from "@/features/workflows/nodeTypes";
import { cn } from "@/lib/cn";
import { nodeLabel } from "./labels";
import { BranchRows, isBranching } from "./nodes/BranchRows";
import { NodeSummary } from "./nodes/NodeSummary";
import { NodeRunIndicator, SingleRunButton } from "./run/NodeRunIndicator";

export type CardNode = Node<FlowNode, "card">;

export function IssueDot({ counts }: { counts: IssueCount | undefined }) {
  const { t } = useTranslation("workflows");
  if (!counts || (counts.errors === 0 && counts.warnings === 0)) return null;
  const error = counts.errors > 0;
  const label = error
    ? t("card.errors", { count: counts.errors })
    : t("card.warnings", { count: counts.warnings });
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn("size-2.5 shrink-0 rounded-full", error ? "bg-danger" : "bg-warn")}
    />
  );
}

export function useImplemented(type: string): boolean {
  const nodeTypes = useNodeTypes();
  const info = nodeTypes.data?.find((item) => item.type === type);
  return info?.implemented ?? true;
}

// 노드 카드 공통 틀: 타입 아이콘·이름·id·요약 한 줄·검증 표시·실행 상태·핸들(설계 7.1, 7.3).
export const NodeCard = memo(function NodeCard({ id, data: node, selected }: NodeProps<CardNode>) {
  const { t } = useTranslation("workflows");
  const counts = useFlow((state) => state.issueCounts.get(id));
  const implemented = useImplemented(node.type);
  const meta = nodeMeta(node.type);
  const Icon = meta.icon;
  const branching = isBranching(node.type);
  const label = nodeLabel(t, node.type);
  // 서버가 초안을 저장할 때 계산한 미리 흘리기 상류(설계 3.8). 사이에 분기가 있으면 비어 있다.
  const streamed = node.type === ANSWER_TYPE ? prestreamSources(node) : [];
  return (
    <div
      className={cn(
        "group w-60 rounded-lg border bg-surface shadow-sm transition-colors",
        selected && "is-selected",
        selected
          ? "border-accent ring-2 ring-focus"
          : counts?.errors
            ? "border-danger"
            : "border-line hover:border-line-strong",
      )}
      data-testid={`flow-node-${id}`}
    >
      {hasTargetHandle(node) ? (
        <Handle type="target" position={Position.Left} id={IN_HANDLE} />
      ) : null}
      <div className="flex items-center gap-2 px-3 pt-2.5 pb-1">
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
          <p className="truncate font-mono text-[11px] leading-tight text-ink-subtle">{id}</p>
        </div>
        <SingleRunButton nodeId={id} type={node.type} />
        <NodeRunIndicator nodeId={id} />
        <IssueDot counts={counts} />
      </div>
      <div className="px-3 pb-2 text-xs text-ink-muted">
        {!branching ? <NodeSummary node={node} /> : null}
        {!implemented ? (
          <Badge tone="warn" className="mt-1">
            {t("card.notImplemented")}
          </Badge>
        ) : null}
        {isImportedCode(node) ? (
          <Badge tone="warn" className="mt-1" title={t("dsl.review.badgeHint")}>
            {t("dsl.review.badge")}
          </Badge>
        ) : null}
        {streamed.length > 0 ? (
          <Badge
            tone="info"
            className="mt-1"
            title={t("card.prestreamHint", { sources: streamed.join(", ") })}
          >
            {t("card.prestream")}
          </Badge>
        ) : null}
      </div>
      {branching ? <BranchRows node={node} /> : null}
      {!branching && node.type !== "end" ? (
        <Handle type="source" position={Position.Right} id={OUT_HANDLE} />
      ) : null}
      {node.error.strategy === "branch" ? (
        <div className="relative border-t border-line px-3 py-1.5 text-xs text-danger">
          {t("card.fail")}
          <Handle type="source" position={Position.Right} id={FAIL_HANDLE} />
        </div>
      ) : null}
    </div>
  );
});
