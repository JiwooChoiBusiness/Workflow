import { useReactFlow } from "@xyflow/react";
import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "@/components/ui/toast";
import { useFlowStoreApi } from "@/features/workflows/flowStoreContext";
import {
  freePosition,
  ITERATION_TYPE,
  MAX_NODES,
  nodeById,
  START_TYPE,
} from "@/features/workflows/graph";
import { createNodes, ITERATION_SIZE } from "@/features/workflows/nodeTypes";
import { nodeLabel } from "./labels";

export interface AddNodeOptions {
  parentId?: string | null;
  // 이 노드의 핸들에서 새 노드로 이어 준다.
  connectFrom?: { source: string; handle: string };
}

const CARD = { width: 240, height: 90 };
// 반복 노드 머리(제목 줄) 아래부터 안쪽 노드를 둔다.
const INSET = { left: 12, top: 56, right: 12, bottom: 12 };

// 팔레트(누르기·끌어 놓기)가 새 노드를 만드는 길. 이름은 타입 이름에 같은 타입 수를 붙인다(LLM 2).
export function useAddNode() {
  const { t } = useTranslation("workflows");
  const store = useFlowStoreApi();
  const { getInternalNode, getViewport, setCenter } = useReactFlow();

  // 새 노드가 화면 밖이면 그 노드가 가운데 오게 옮긴다(배율은 그대로).
  const reveal = useCallback(
    (id: string) => {
      const internal = getInternalNode(id);
      const pane = document.querySelector(".wf-flow")?.getBoundingClientRect();
      if (!internal || !pane) return;
      const { x, y, zoom } = getViewport();
      const position = internal.internals.positionAbsolute;
      const width = internal.measured.width ?? CARD.width;
      const height = internal.measured.height ?? CARD.height;
      const left = position.x * zoom + x;
      const top = position.y * zoom + y;
      const inside =
        left >= 0 &&
        top >= 0 &&
        left + width * zoom <= pane.width &&
        top + height * zoom <= pane.height;
      if (!inside) {
        void setCenter(position.x + width / 2, position.y + height / 2, { zoom, duration: 200 });
      }
    },
    [getInternalNode, getViewport, setCenter],
  );

  return useCallback(
    (type: string, position: { x: number; y: number }, options: AddNodeOptions = {}) => {
      const state = store.getState();
      const extra = type === ITERATION_TYPE ? 2 : 1;
      if (state.graph.nodes.length + extra > MAX_NODES) {
        toast.error(t("editor.tooManyNodes", { max: MAX_NODES }));
        return null;
      }
      if (type === START_TYPE && state.graph.nodes.some((node) => node.type === START_TYPE)) {
        toast.error(t("palette.startExists"));
        return null;
      }
      // 반복 안에 반복은 둘 수 없다(서버 nested_iteration).
      const parentId = type === ITERATION_TYPE ? null : (options.parentId ?? null);
      let placed = freePosition(state.graph, position, parentId, state.dimensions);
      const parent = parentId ? nodeById(state.graph, parentId) : undefined;
      if (parent) {
        // 안쪽 노드는 반복 노드 틀 안에 둔다(ReactFlow extent=parent는 끌 때만 막는다).
        const width = Number(parent.ui.width ?? ITERATION_SIZE.width);
        const height = Number(parent.ui.height ?? ITERATION_SIZE.height);
        placed = {
          x: Math.min(Math.max(placed.x, INSET.left), width - CARD.width - INSET.right),
          y: Math.min(Math.max(placed.y, INSET.top), height - CARD.height - INSET.bottom),
        };
      }
      const label = nodeLabel(t, type);
      const sameType = state.graph.nodes.filter((node) => node.type === type).length;
      const nodes = createNodes(state.graph, {
        type,
        kind: state.kind,
        title: sameType > 0 ? `${label} ${sameType + 1}` : label,
        position: placed,
        parentId,
        labels: { className: (n) => t("forms.classifier.defaultName", { n }) },
        iterationStartTitle: nodeLabel(t, "iteration_start"),
      });
      state.actions.addNodes(nodes, options.connectFrom);
      const id = nodes[0]?.id ?? null;
      // 카드가 그려지고 크기를 잰 뒤에 본다.
      if (id) requestAnimationFrame(() => requestAnimationFrame(() => reveal(id)));
      return id;
    },
    [store, t, reveal],
  );
}
