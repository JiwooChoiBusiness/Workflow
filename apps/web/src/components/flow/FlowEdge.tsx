import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";
import { X } from "lucide-react";
import { memo } from "react";
import { useTranslation } from "react-i18next";
import { useFlowActions } from "@/features/workflows/flowStoreContext";

// 엣지 모양은 FlowCanvas가 붙이는 클래스(실패 가지 wf-edge-fail, 검증 오류 wf-edge-issue)로 정한다.
// 고른 엣지에는 가운데에 지우기 단추를 둔다.
export const FlowEdge = memo(function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
  markerEnd,
}: EdgeProps) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={18} />
      {selected ? (
        <EdgeLabelRenderer>
          <button
            type="button"
            className="nodrag nopan absolute grid size-5 place-items-center rounded-full border border-line bg-surface text-ink-muted shadow-sm hover:border-danger hover:text-danger"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "all",
            }}
            aria-label={t("canvas.removeEdge")}
            title={t("canvas.removeEdge")}
            onClick={() => actions.remove([], [id])}
          >
            <X className="size-3" aria-hidden />
          </button>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
});
