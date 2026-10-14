import {
  Background,
  BackgroundVariant,
  MarkerType,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type IsValidConnection,
  type Node,
  type NodeChange,
  type OnDelete,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { useCallback, useMemo, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "@/components/ui/toast";
import type { Dimensions } from "@/features/workflows/flowStore";
import { useFlow, useFlowActions, useFlowStoreApi } from "@/features/workflows/flowStoreContext";
import {
  connectProblem,
  FAIL_HANDLE,
  ITERATION_TYPE,
  parentsFirst,
  type FlowEdge as GraphEdge,
  type FlowNode,
  type IssueCount,
} from "@/features/workflows/graph";
import { ITERATION_SIZE } from "@/features/workflows/nodeTypes";
import { CanvasToolbar } from "./CanvasToolbar";
import { FlowEdge } from "./FlowEdge";
import "./flow.css";
import { NodeCard } from "./NodeCard";
import { IterationNode } from "./nodes/IterationNode";
import { useAddNode } from "./useAddNode";

export { ReactFlowProvider as FlowCanvasProvider } from "@xyflow/react";

// 팔레트에서 끌어 온 노드 타입을 싣는 dataTransfer 형식
export const NODE_DRAG_TYPE = "application/x-wf-node-type";

const nodeTypes = { card: NodeCard, iteration: IterationNode };
const edgeTypes = { flow: FlowEdge };
const markerEnd = { type: MarkerType.ArrowClosed, width: 14, height: 14 };

interface CachedNode {
  selected: boolean;
  dims: Dimensions | undefined;
  rf: Node;
}

// 바뀌지 않은 노드는 같은 ReactFlow 노드 객체를 돌려줘 카드가 다시 그려지지 않게 한다(노드 100개 끌기).
const nodeCache = new WeakMap<FlowNode, CachedNode>();

function iterationSize(node: FlowNode): Dimensions {
  const width = typeof node.ui.width === "number" ? node.ui.width : ITERATION_SIZE.width;
  const height = typeof node.ui.height === "number" ? node.ui.height : ITERATION_SIZE.height;
  return { width, height };
}

function toRfNodes(
  nodes: FlowNode[],
  selection: string[],
  dims: Record<string, Dimensions>,
): Node[] {
  const selected = new Set(selection);
  return parentsFirst(nodes).map((node) => {
    const isSelected = selected.has(node.id);
    const measured = dims[node.id];
    const hit = nodeCache.get(node);
    if (hit && hit.selected === isSelected && hit.dims === measured) return hit.rf;
    const iteration = node.type === ITERATION_TYPE;
    const size = iteration ? iterationSize(node) : null;
    const rf: Node = {
      id: node.id,
      type: iteration ? "iteration" : "card",
      position: node.position,
      data: node,
      selected: isSelected,
      ...(size ? { width: size.width, height: size.height } : {}),
      ...(measured ? { measured } : {}),
      ...(node.parent_id ? { parentId: node.parent_id, extent: "parent" as const } : {}),
    };
    nodeCache.set(node, { selected: isSelected, dims: measured, rf });
    return rf;
  });
}

interface CachedEdge {
  selected: boolean;
  issue: boolean;
  rf: Edge;
}

const edgeCache = new WeakMap<GraphEdge, CachedEdge>();

function toRfEdges(
  edges: GraphEdge[],
  selection: string[],
  issues: Map<string, IssueCount>,
): Edge[] {
  const selected = new Set(selection);
  return edges.map((edge) => {
    const isSelected = selected.has(edge.id);
    const issue = (issues.get(`edge:${edge.id}`)?.errors ?? 0) > 0;
    const hit = edgeCache.get(edge);
    if (hit && hit.selected === isSelected && hit.issue === issue) return hit.rf;
    const classes = [
      edge.source_handle === FAIL_HANDLE ? "wf-edge-fail" : "",
      issue ? "wf-edge-issue" : "",
    ].filter(Boolean);
    const rf: Edge = {
      id: edge.id,
      type: "flow",
      source: edge.source,
      sourceHandle: edge.source_handle,
      target: edge.target,
      targetHandle: edge.target_handle,
      selected: isSelected,
      markerEnd,
      ...(classes.length ? { className: classes.join(" ") } : {}),
    };
    edgeCache.set(edge, { selected: isSelected, issue, rf });
    return rf;
  });
}

function savedViewport(value: Record<string, unknown>): Viewport | undefined {
  const { x, y, zoom } = value;
  if (typeof x === "number" && typeof y === "number" && typeof zoom === "number" && zoom > 0) {
    return { x, y, zoom };
  }
  return undefined;
}

export function FlowCanvas() {
  const { t } = useTranslation("workflows");
  const store = useFlowStoreApi();
  const actions = useFlowActions();
  const graph = useFlow((state) => state.graph);
  const selection = useFlow((state) => state.selection);
  const selectedEdges = useFlow((state) => state.selectedEdges);
  const dims = useFlow((state) => state.dimensions);
  const issueCounts = useFlow((state) => state.issueCounts);
  // 처음 그릴 때만 쓰는 화면 위치. 그 뒤로는 ReactFlow가 들고 있고 바뀌면 store에 적는다.
  const viewport = useFlow((state) => state.viewport);
  const initialViewport = useMemo(() => savedViewport(viewport), [viewport]);
  const { screenToFlowPosition, getInternalNode } = useReactFlow();
  const addNode = useAddNode();

  const nodes = useMemo(
    () => toRfNodes(graph.nodes, selection, dims),
    [graph.nodes, selection, dims],
  );
  const edges = useMemo(
    () => toRfEdges(graph.edges, selectedEdges, issueCounts),
    [graph.edges, selectedEdges, issueCounts],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      const state = store.getState();
      const moves: { id: string; position: { x: number; y: number } }[] = [];
      const measured: { id: string; dimensions: Dimensions }[] = [];
      let selected: Set<string> | null = null;
      for (const change of changes) {
        if (change.type === "position" && change.position) {
          moves.push({ id: change.id, position: change.position });
        } else if (change.type === "dimensions" && change.dimensions) {
          measured.push({ id: change.id, dimensions: change.dimensions });
          // 크기 조절 손잡이(NodeResizer)에서 온 변경만 그래프(ui)에 적는다.
          if (change.setAttributes) state.actions.resizeNode(change.id, change.dimensions);
        } else if (change.type === "select") {
          selected ??= new Set(state.selection);
          if (change.selected) selected.add(change.id);
          else selected.delete(change.id);
        }
      }
      if (measured.length) state.actions.setDimensions(measured);
      if (moves.length) {
        // 끌기 밖의 이동(방향키)은 한 번이 실행 취소 한 번이다.
        const discrete = store.getState().moveStart === null;
        if (discrete) state.actions.beginMove();
        state.actions.moveNodes(moves);
        if (discrete) state.actions.endMove();
      }
      if (selected) state.actions.select([...selected], store.getState().selectedEdges);
    },
    [store],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const state = store.getState();
      let selected: Set<string> | null = null;
      for (const change of changes) {
        if (change.type !== "select") continue;
        selected ??= new Set(state.selectedEdges);
        if (change.selected) selected.add(change.id);
        else selected.delete(change.id);
      }
      if (selected) state.actions.select(store.getState().selection, [...selected]);
    },
    [store],
  );

  // Delete·Backspace로 지울 때 한 번에 온다. 노드와 이어진 엣지도 함께 온다.
  const onDelete: OnDelete = useCallback(
    ({ nodes: removed, edges: removedEdges }) => {
      actions.remove(
        removed.map((node) => node.id),
        removedEdges.map((edge) => edge.id),
      );
    },
    [actions],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const problem = actions.connect(connection);
      if (problem) toast.error(t(`canvas.connect.${problem}`));
    },
    [actions, t],
  );

  const isValidConnection: IsValidConnection = useCallback(
    (connection) =>
      connectProblem(store.getState().graph, {
        source: connection.source,
        sourceHandle: connection.sourceHandle,
        target: connection.target,
      }) === null,
    [store],
  );

  const onDragOver = useCallback((event: DragEvent) => {
    if (!event.dataTransfer.types.includes(NODE_DRAG_TYPE)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      const type = event.dataTransfer.getData(NODE_DRAG_TYPE);
      if (!type) return;
      event.preventDefault();
      const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      // 반복 노드 안에 놓으면 그 하위 그래프에 넣는다. 좌표는 반복 노드 기준이다.
      const container = store
        .getState()
        .graph.nodes.filter((node) => node.type === ITERATION_TYPE && !node.parent_id)
        .find((node) => {
          const size = iterationSize(node);
          const origin = getInternalNode(node.id)?.internals.positionAbsolute ?? node.position;
          return (
            point.x >= origin.x &&
            point.x <= origin.x + size.width &&
            point.y >= origin.y &&
            point.y <= origin.y + size.height
          );
        });
      if (container && type !== ITERATION_TYPE) {
        const origin =
          getInternalNode(container.id)?.internals.positionAbsolute ?? container.position;
        addNode(type, { x: point.x - origin.x, y: point.y - origin.y }, { parentId: container.id });
      } else {
        addNode(type, point);
      }
    },
    [addNode, getInternalNode, screenToFlowPosition, store],
  );

  return (
    <section aria-label={t("canvas.label")} className="h-full w-full">
      <ReactFlow
        className="wf-flow"
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onDelete={onDelete}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeDragStart={actions.beginMove}
        onNodeDragStop={actions.endMove}
        onSelectionDragStart={actions.beginMove}
        onSelectionDragStop={actions.endMove}
        onMoveEnd={(_event, viewport) => actions.setViewport({ ...viewport })}
        onDragOver={onDragOver}
        onDrop={onDrop}
        deleteKeyCode={["Backspace", "Delete"]}
        defaultViewport={initialViewport}
        fitView={initialViewport === undefined}
        fitViewOptions={{ maxZoom: 1, padding: 0.2 }}
        minZoom={0.2}
        maxZoom={2}
        colorMode="system"
        attributionPosition="top-right"
        defaultEdgeOptions={{ type: "flow" }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} />
        <MiniMap pannable zoomable ariaLabel={t("canvas.miniMap")} />
        <CanvasToolbar />
      </ReactFlow>
    </section>
  );
}
