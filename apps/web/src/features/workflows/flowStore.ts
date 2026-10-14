/*
 * 캔버스 편집기의 상태(설계 7.3 flowStore). 그래프가 단일 출처이고 ReactFlow의 nodes·edges, 속성 패널,
 * 자동 저장이 모두 여기서 읽는다. 실행 취소는 그래프만 50단계까지 되돌린다(환경 변수·설정은 제외).
 */
import { createStore, type StoreApi } from "zustand/vanilla";
import type {
  ConversationVar,
  WfIssue,
  WorkflowKind,
  WorkflowSettings,
  WorkflowVersionOut,
} from "@/api/types";
import {
  connectProblem,
  countIssues,
  nextEdgeId,
  nodeById,
  normalizeGraph,
  pruneHandleEdges,
  removeElements,
  renameNode,
  IN_HANDLE,
  OUT_HANDLE,
  type ConnectionLike,
  type ConnectProblem,
  type FlowGraph,
  type FlowNode,
  type IssueCount,
} from "./graph";

export const HISTORY_LIMIT = 50;
// 같은 칸을 이 시간 안에 이어서 고치면(글자 입력) 실행 취소 한 번으로 묶는다.
export const COALESCE_MS = 1000;

export interface EnvRow {
  name: string;
  value: string;
  secret: boolean;
  // 서버에 값이 있지만 응답에 오지 않은 비밀 값. 값 칸이 비어 있으면 그 값을 그대로 둔다.
  keep: boolean;
}

export interface Dimensions {
  width: number;
  height: number;
}

export type NodePatch = Partial<Pick<FlowNode, "title" | "error" | "retry" | "ui" | "position">>;

export interface FlowActions {
  load: (draft: WorkflowVersionOut) => void;
  select: (nodeIds: string[], edgeIds?: string[]) => void;
  addNodes: (nodes: FlowNode[], connectFrom?: { source: string; handle: string }) => void;
  connect: (connection: ConnectionLike) => ConnectProblem | null;
  remove: (nodeIds: string[], edgeIds: string[]) => void;
  // 끌기 한 번이 실행 취소 한 번이다. 시작할 때 되돌릴 자리를 만들고, 움직임이 없었으면 끝날 때 버린다.
  beginMove: () => void;
  moveNodes: (moves: { id: string; position: { x: number; y: number } }[]) => void;
  // 반복 노드의 크기 조절. 끌기처럼 beginMove·endMove 사이에서 실행 취소 자리 없이 바꾼다.
  resizeNode: (id: string, size: Dimensions) => void;
  endMove: () => void;
  setDimensions: (changes: { id: string; dimensions: Dimensions }[]) => void;
  updateConfig: (nodeId: string, config: Record<string, unknown>) => void;
  updateNode: (nodeId: string, patch: NodePatch, coalesceKey?: string) => void;
  rename: (from: string, to: string) => void;
  undo: () => void;
  redo: () => void;
  setEnvVars: (rows: EnvRow[]) => void;
  setConversationVars: (vars: ConversationVar[]) => void;
  setSettings: (settings: WorkflowSettings) => void;
  setViewport: (viewport: Record<string, unknown>) => void;
  setIssues: (issues: WfIssue[]) => void;
  markSaved: (revision: number, saved: WorkflowVersionOut) => void;
}

export interface FlowState {
  workflowId: string;
  kind: WorkflowKind;
  graph: FlowGraph;
  viewport: Record<string, unknown>;
  envVars: EnvRow[];
  conversationVars: ConversationVar[];
  settings: WorkflowSettings;
  selection: string[];
  selectedEdges: string[];
  issues: WfIssue[];
  // 노드 id(엣지는 "edge:<id>")별 오류·경고 수. 카드가 자기 것만 구독한다.
  issueCounts: Map<string, IssueCount>;
  // 낙관적 잠금 값(GET·PUT draft의 updated_at)
  updatedAt: string;
  // 바뀔 때마다 1씩 오른다. 저장한 시점의 값과 다르면 저장하지 않은 변경이 있다.
  revision: number;
  savedRevision: number;
  past: FlowGraph[];
  future: FlowGraph[];
  // 실행 취소·id 변경처럼 폼 밖에서 config가 바뀌면 오른다. 속성 패널이 이 값으로 폼을 다시 만든다.
  formEpoch: number;
  // 초안을 (다시) 불러올 때마다 오른다. 환경 변수·설정 폼이 이 값으로 새로 시작한다.
  loadCount: number;
  // ReactFlow가 잰 노드 크기. 그래프에 저장하지 않는다.
  dimensions: Record<string, Dimensions>;
  coalesceKey: string | null;
  coalesceAt: number;
  moveStart: FlowGraph | null;
  actions: FlowActions;
}

export type FlowStore = StoreApi<FlowState>;

export function envRowsFromDraft(draft: WorkflowVersionOut): EnvRow[] {
  return draft.env_vars.map((item) => ({
    name: item.name,
    value: item.value ?? "",
    secret: item.secret,
    keep: item.value === null && item.has_value,
  }));
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

// 분기 핸들이 config나 오류 처리에서 나오는 노드. 핸들이 사라지면 그 엣지를 지운다.
function branchesChanged(before: FlowNode, after: FlowNode): boolean {
  return (
    before.error.strategy !== after.error.strategy ||
    ((after.type === "if_else" || after.type === "question_classifier") &&
      !sameJson(
        before.config.cases ?? before.config.classes,
        after.config.cases ?? after.config.classes,
      ))
  );
}

export function createFlowStore(
  workflowId: string,
  kind: WorkflowKind,
  now: () => number = Date.now,
) {
  return createStore<FlowState>()((set, get) => {
    // 그래프를 바꾸고 실행 취소 자리를 남긴다. key가 같고 COALESCE_MS 안이면 앞의 자리에 묶는다.
    function commit(next: FlowGraph, key: string | null = null, extra: Partial<FlowState> = {}) {
      const state = get();
      if (next === state.graph) return;
      const at = now();
      const merge =
        key !== null && key === state.coalesceKey && at - state.coalesceAt < COALESCE_MS;
      set({
        graph: next,
        past: merge ? state.past : [...state.past, state.graph].slice(-HISTORY_LIMIT),
        future: [],
        coalesceKey: key,
        coalesceAt: at,
        revision: state.revision + 1,
        ...extra,
      });
    }

    function keepExisting(ids: string[], graph: FlowGraph): string[] {
      return ids.filter((id) => graph.nodes.some((node) => node.id === id));
    }

    function restore(graph: FlowGraph, past: FlowGraph[], future: FlowGraph[]) {
      const state = get();
      set({
        graph,
        past,
        future,
        coalesceKey: null,
        moveStart: null,
        revision: state.revision + 1,
        formEpoch: state.formEpoch + 1,
        selection: keepExisting(state.selection, graph),
        selectedEdges: state.selectedEdges.filter((id) =>
          graph.edges.some((edge) => edge.id === id),
        ),
      });
    }

    const actions: FlowActions = {
      load(draft) {
        const graph = normalizeGraph(draft.graph);
        set({
          graph,
          viewport: graph.viewport,
          envVars: envRowsFromDraft(draft),
          conversationVars: draft.conversation_vars,
          settings: draft.settings,
          issues: draft.issues,
          issueCounts: countIssues(draft.issues),
          updatedAt: draft.updated_at,
          revision: 0,
          savedRevision: 0,
          past: [],
          future: [],
          coalesceKey: null,
          moveStart: null,
          formEpoch: get().formEpoch + 1,
          loadCount: get().loadCount + 1,
          selection: keepExisting(get().selection, graph),
          selectedEdges: [],
        });
      },
      select(nodeIds, edgeIds = []) {
        const state = get();
        if (sameJson(state.selection, nodeIds) && sameJson(state.selectedEdges, edgeIds)) return;
        set({ selection: nodeIds, selectedEdges: edgeIds });
      },
      addNodes(nodes, connectFrom) {
        const graph = get().graph;
        let next: FlowGraph = { ...graph, nodes: [...graph.nodes, ...nodes] };
        const first = nodes[0];
        if (connectFrom && first) {
          const connection = {
            source: connectFrom.source,
            sourceHandle: connectFrom.handle,
            target: first.id,
          };
          if (connectProblem(next, connection) === null) {
            next = {
              ...next,
              edges: [
                ...next.edges,
                {
                  id: nextEdgeId(next),
                  source: connectFrom.source,
                  source_handle: connectFrom.handle,
                  target: first.id,
                  target_handle: IN_HANDLE,
                },
              ],
            };
          }
        }
        commit(next, null, { selection: first ? [first.id] : [], selectedEdges: [] });
      },
      connect(connection) {
        const graph = get().graph;
        const problem = connectProblem(graph, connection);
        if (problem) return problem;
        commit({
          ...graph,
          edges: [
            ...graph.edges,
            {
              id: nextEdgeId(graph),
              source: connection.source,
              source_handle: connection.sourceHandle || OUT_HANDLE,
              target: connection.target,
              target_handle: IN_HANDLE,
            },
          ],
        });
        return null;
      },
      remove(nodeIds, edgeIds) {
        const state = get();
        const next = removeElements(state.graph, nodeIds, edgeIds);
        commit(next, null, {
          selection: keepExisting(state.selection, next),
          selectedEdges: state.selectedEdges.filter((id) =>
            next.edges.some((edge) => edge.id === id),
          ),
        });
      },
      beginMove() {
        set({ moveStart: get().graph });
      },
      moveNodes(moves) {
        const state = get();
        const byId = new Map(moves.map((move) => [move.id, move.position]));
        let changed = false;
        const nodes = state.graph.nodes.map((node) => {
          const position = byId.get(node.id);
          if (
            !position ||
            (Math.round(position.x) === node.position.x &&
              Math.round(position.y) === node.position.y)
          ) {
            return node;
          }
          changed = true;
          return { ...node, position: { x: Math.round(position.x), y: Math.round(position.y) } };
        });
        if (!changed) return;
        // 끌기 중에는 실행 취소 자리를 쌓지 않는다(beginMove가 한 번 만든다).
        set({ graph: { ...state.graph, nodes }, revision: state.revision + 1 });
      },
      resizeNode(id, size) {
        const state = get();
        const node = nodeById(state.graph, id);
        if (!node) return;
        const width = Math.round(size.width);
        const height = Math.round(size.height);
        if (node.ui.width === width && node.ui.height === height) return;
        const updated = { ...node, ui: { ...node.ui, width, height } };
        set({
          graph: {
            ...state.graph,
            nodes: state.graph.nodes.map((item) => (item.id === id ? updated : item)),
          },
          revision: state.revision + 1,
        });
      },
      endMove() {
        const state = get();
        const start = state.moveStart;
        if (!start) return;
        const moved = start.nodes.some((node, index) => node !== state.graph.nodes[index]);
        set({
          moveStart: null,
          coalesceKey: null,
          past: moved ? [...state.past, start].slice(-HISTORY_LIMIT) : state.past,
          future: moved ? [] : state.future,
        });
      },
      setDimensions(changes) {
        const current = get().dimensions;
        let next: Record<string, Dimensions> | null = null;
        for (const { id, dimensions } of changes) {
          const old = current[id];
          if (old && old.width === dimensions.width && old.height === dimensions.height) continue;
          next ??= { ...current };
          next[id] = dimensions;
        }
        if (next) set({ dimensions: next });
      },
      updateConfig(nodeId, config) {
        const graph = get().graph;
        const node = nodeById(graph, nodeId);
        if (!node || sameJson(node.config, config)) return;
        const updated = { ...node, config };
        let next: FlowGraph = {
          ...graph,
          nodes: graph.nodes.map((item) => (item.id === nodeId ? updated : item)),
        };
        if (branchesChanged(node, updated)) next = pruneHandleEdges(next, nodeId);
        commit(next, `config:${nodeId}`);
      },
      updateNode(nodeId, patch, coalesceKey) {
        const graph = get().graph;
        const node = nodeById(graph, nodeId);
        if (!node) return;
        const updated = { ...node, ...patch };
        if (sameJson(node, updated)) return;
        let next: FlowGraph = {
          ...graph,
          nodes: graph.nodes.map((item) => (item.id === nodeId ? updated : item)),
        };
        if (branchesChanged(node, updated)) next = pruneHandleEdges(next, nodeId);
        commit(next, coalesceKey ?? null);
      },
      rename(from, to) {
        const state = get();
        if (from === to || !nodeById(state.graph, from)) return;
        const next = renameNode(state.graph, from, to);
        const dimensions = { ...state.dimensions };
        if (dimensions[from]) {
          dimensions[to] = dimensions[from];
          delete dimensions[from];
        }
        commit(next, null, {
          formEpoch: state.formEpoch + 1,
          selection: state.selection.map((id) => (id === from ? to : id)),
          dimensions,
        });
      },
      undo() {
        const { past, future, graph } = get();
        const previous = past.at(-1);
        if (!previous) return;
        restore(previous, past.slice(0, -1), [graph, ...future]);
      },
      redo() {
        const { past, future, graph } = get();
        const next = future[0];
        if (!next) return;
        restore(next, [...past, graph].slice(-HISTORY_LIMIT), future.slice(1));
      },
      setEnvVars(rows) {
        if (sameJson(get().envVars, rows)) return;
        set({ envVars: rows, revision: get().revision + 1 });
      },
      setConversationVars(vars) {
        if (sameJson(get().conversationVars, vars)) return;
        set({ conversationVars: vars, revision: get().revision + 1 });
      },
      setSettings(settings) {
        if (sameJson(get().settings, settings)) return;
        set({ settings, revision: get().revision + 1 });
      },
      setViewport(viewport) {
        // 화면 이동만으로는 저장하지 않는다. 다음 저장에 함께 실린다.
        set({ viewport });
      },
      setIssues(issues) {
        set({ issues, issueCounts: countIssues(issues) });
      },
      markSaved(revision, saved) {
        set({
          savedRevision: revision,
          updatedAt: saved.updated_at,
          issues: saved.issues,
          issueCounts: countIssues(saved.issues),
        });
      },
    };

    return {
      workflowId,
      kind,
      graph: { nodes: [], edges: [], viewport: {} },
      viewport: {},
      envVars: [],
      conversationVars: [],
      settings: {
        approval_mode: "inherit",
        mask_secrets_in_outputs: true,
        limits: { max_run_seconds: 1800, max_total_tokens: 2_000_000, max_cost_usd: null },
        webapp: { suggested_questions: [] },
      },
      selection: [],
      selectedEdges: [],
      issues: [],
      issueCounts: new Map(),
      updatedAt: "",
      revision: 0,
      savedRevision: 0,
      past: [],
      future: [],
      formEpoch: 0,
      loadCount: 0,
      dimensions: {},
      coalesceKey: null,
      coalesceAt: 0,
      moveStart: null,
      actions,
    };
  });
}

export function isDirty(state: Pick<FlowState, "revision" | "savedRevision">): boolean {
  return state.revision !== state.savedRevision;
}
