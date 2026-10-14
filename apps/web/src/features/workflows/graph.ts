/*
 * 워크플로우 그래프(설계 3.1)를 화면에서 다루는 함수들. 상류 계산·핸들·연결 규칙은 서버 검증
 * (apps/api/app/workflow/graph.py)과 같은 규칙을 따르고, 최종 판정은 서버 검증이 한다.
 */
import type {
  NodeTypeInfo,
  VarType,
  WfEdgeOut,
  WfGraphOut,
  WfIssue,
  WfNodeOut,
  WorkflowKind,
} from "@/api/types";

export type ErrorStrategy = "fail" | "default" | "branch";

// ReactFlow 노드의 data로 그대로 넘긴다(바뀌지 않은 노드는 같은 객체라 카드가 다시 그려지지 않는다).
// data는 Record<string, unknown>이어야 해서 interface가 아니라 type으로 둔다.
export type FlowNode = {
  id: string;
  type: string;
  version: number;
  title: string;
  config: Record<string, unknown>;
  error: { strategy: ErrorStrategy; default_outputs: Record<string, unknown> };
  retry: { max_attempts: number; interval_ms: number };
  // iteration 하위 그래프에 속하면 그 iteration 노드 id. 위치는 부모 기준 좌표다(ReactFlow 하위 흐름과 같다).
  parent_id: string | null;
  position: { x: number; y: number };
  ui: Record<string, unknown>;
};

export interface FlowEdge {
  id: string;
  source: string;
  source_handle: string;
  target: string;
  target_handle: string;
}

export interface FlowGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
  viewport: Record<string, unknown>;
}

export const OUT_HANDLE = "out";
export const IN_HANDLE = "in";
export const FAIL_HANDLE = "fail";
export const ELSE_HANDLE = "else";
export const START_TYPE = "start";
export const END_TYPE = "end";
export const ANSWER_TYPE = "answer";
export const ITERATION_TYPE = "iteration";
export const ITERATION_START_TYPE = "iteration_start";
export const MAX_NODES = 100;
export const MAX_EDGES = 300;
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
export const RESERVED_SCOPES: ReadonlySet<string> = new Set(["sys", "env", "conv"]);
const ENTRY_TYPES: ReadonlySet<string> = new Set([START_TYPE, ITERATION_START_TYPE]);

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function finite(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function normalizeNode(raw: WfNodeOut): FlowNode {
  const error = record(raw.error);
  const retry = record(raw.retry);
  const position = record(raw.position);
  const strategy = error.strategy;
  return {
    id: raw.id,
    type: raw.type,
    version: finite(raw.version, 1),
    title: raw.title ?? "",
    config: record(raw.config),
    error: {
      strategy: strategy === "default" || strategy === "branch" ? strategy : "fail",
      default_outputs: record(error.default_outputs),
    },
    retry: {
      max_attempts: finite(retry.max_attempts, 0),
      interval_ms: finite(retry.interval_ms, 1000),
    },
    parent_id: raw.parent_id ?? null,
    position: { x: finite(position.x, 0), y: finite(position.y, 0) },
    ui: record(raw.ui),
  };
}

export function normalizeEdge(raw: WfEdgeOut): FlowEdge {
  return {
    id: raw.id,
    source: raw.source,
    source_handle: raw.source_handle || OUT_HANDLE,
    target: raw.target,
    target_handle: raw.target_handle || IN_HANDLE,
  };
}

export function normalizeGraph(raw: WfGraphOut | null | undefined): FlowGraph {
  return {
    nodes: (raw?.nodes ?? []).map(normalizeNode),
    edges: (raw?.edges ?? []).map(normalizeEdge),
    viewport: record(raw?.viewport),
  };
}

export function nodeById(graph: FlowGraph, id: string): FlowNode | undefined {
  return graph.nodes.find((node) => node.id === id);
}

function ids(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const id = record(item).id;
    return typeof id === "string" || typeof id === "number" ? [String(id)] : [];
  });
}

// 노드에서 나가는 핸들. 서버 NodeRunner.handles + error.strategy=branch면 fail.
export function sourceHandles(node: FlowNode): string[] {
  let handles: string[];
  switch (node.type) {
    case END_TYPE:
      handles = [];
      break;
    case "if_else":
      handles = [...ids(node.config.cases).map((id) => `case_${id}`), ELSE_HANDLE];
      break;
    case "question_classifier":
      handles = ids(node.config.classes).map((id) => `class_${id}`);
      break;
    default:
      handles = [OUT_HANDLE];
  }
  return node.error.strategy === "branch" ? [...handles, FAIL_HANDLE] : handles;
}

export function hasTargetHandle(node: FlowNode): boolean {
  return !ENTRY_TYPES.has(node.type);
}

// 서버 graph.py의 구조 분석에 쓰는 엣지와 같다. 없는 노드, 다른 프레임, 진입 노드로 들어오는 엣지는 뺀다.
function frameEdges(graph: FlowGraph): FlowEdge[] {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  return graph.edges.filter((edge) => {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    return (
      source !== undefined &&
      target !== undefined &&
      !ENTRY_TYPES.has(target.type) &&
      source.parent_id === target.parent_id
    );
  });
}

function incomingMap(graph: FlowGraph): Map<string, string[]> {
  const incoming = new Map<string, string[]>();
  for (const edge of frameEdges(graph)) {
    const list = incoming.get(edge.target);
    if (list) list.push(edge.source);
    else incoming.set(edge.target, [edge.source]);
  }
  return incoming;
}

// 같은 프레임의 조상을 가까운 것부터. 순환이 있어도 끝난다.
function ancestors(incoming: Map<string, string[]>, nodeId: string): string[] {
  const seen = new Set<string>([nodeId]);
  const order: string[] = [];
  const queue = [...(incoming.get(nodeId) ?? [])];
  while (queue.length > 0) {
    const current = queue.shift()!;
    if (seen.has(current)) continue;
    seen.add(current);
    order.push(current);
    queue.push(...(incoming.get(current) ?? []));
  }
  return order;
}

/*
 * 노드가 참조할 수 있는 상류 노드 id(가까운 것부터). 같은 프레임의 조상이고, iteration 하위 노드는
 * 그 iteration의 바깥 조상도 본다(서버 _Checker.upstream과 같은 규칙).
 */
export function upstreamIds(graph: FlowGraph, nodeId: string): string[] {
  const node = nodeById(graph, nodeId);
  if (!node) return [];
  const incoming = incomingMap(graph);
  const result = ancestors(incoming, nodeId);
  if (node.parent_id) {
    const parent = nodeById(graph, node.parent_id);
    if (parent && parent.parent_id === null) {
      for (const id of ancestors(incoming, parent.id)) {
        if (!result.includes(id)) result.push(id);
      }
    }
  }
  return result;
}

export type ConnectProblem =
  "self" | "missing" | "entry" | "frame" | "handle" | "duplicate" | "cycle" | "limit";

export interface ConnectionLike {
  source: string;
  sourceHandle?: string | null;
  target: string;
  targetHandle?: string | null;
}

// 캔버스에서 이을 수 있는지. 서버가 오류로 보는 연결(다른 프레임, 진입 노드로 들어옴, 없는 핸들, 순환)을 막는다.
export function connectProblem(
  graph: FlowGraph,
  connection: ConnectionLike,
): ConnectProblem | null {
  const { source, target } = connection;
  const handle = connection.sourceHandle || OUT_HANDLE;
  if (source === target) return "self";
  const from = nodeById(graph, source);
  const to = nodeById(graph, target);
  if (!from || !to) return "missing";
  if (!hasTargetHandle(to)) return "entry";
  if (from.parent_id !== to.parent_id) return "frame";
  if (!sourceHandles(from).includes(handle)) return "handle";
  if (
    graph.edges.some(
      (edge) => edge.source === source && edge.source_handle === handle && edge.target === target,
    )
  ) {
    return "duplicate";
  }
  if (graph.edges.length >= MAX_EDGES) return "limit";
  // target에서 source로 가는 길이 이미 있으면 이 엣지가 순환을 만든다.
  if (ancestors(incomingMap(graph), source).includes(target)) return "cycle";
  return null;
}

function numberedMax(values: Iterable<string>, prefix: string): number {
  let max = 0;
  for (const value of values) {
    if (!value.startsWith(prefix)) continue;
    const rest = value.slice(prefix.length);
    if (/^\d+$/.test(rest)) max = Math.max(max, Number(rest));
  }
  return max;
}

// 새 노드 id는 <type>_<n>(설계 2.2). start는 그래프에 하나뿐이라 start를 먼저 쓴다.
export function nextNodeId(graph: FlowGraph, type: string): string {
  const taken = new Set(graph.nodes.map((node) => node.id));
  if (type === START_TYPE && !taken.has(START_TYPE)) return START_TYPE;
  const base = type.slice(0, 28).replace(/_+$/, "") || "node";
  let n = numberedMax(taken, `${base}_`) + 1;
  while (taken.has(`${base}_${n}`)) n += 1;
  return `${base}_${n}`;
}

export function nextEdgeId(graph: FlowGraph): string {
  const taken = new Set(graph.edges.map((edge) => edge.id));
  let n = numberedMax(taken, "e") + 1;
  while (taken.has(`e${n}`)) n += 1;
  return `e${n}`;
}

// 열거형으로 붙는 if_else 케이스·분류 클래스 id. 핸들 이름(case_<id>, class_<id>)이 되므로 바꾸지 않는다.
export function nextItemId(existing: string[]): string {
  let n = numberedMax(existing, "") + 1;
  while (existing.includes(String(n))) n += 1;
  return String(n);
}

export type NodeIdProblem = "pattern" | "reserved" | "taken";

export function nodeIdProblem(
  graph: FlowGraph,
  current: string,
  next: string,
): NodeIdProblem | null {
  if (next === current) return null;
  if (!NODE_ID_PATTERN.test(next)) return "pattern";
  if (RESERVED_SCOPES.has(next)) return "reserved";
  if (graph.nodes.some((node) => node.id === next)) return "taken";
  return null;
}

const SELECTOR =
  /^(?<scope>[a-z][a-z0-9_]{0,31})\.(?<name>[A-Za-z_][A-Za-z0-9_]*)(?<path>(?:\.[^.[\]\s]+|\[\d+\])*)$/;

export interface Selector {
  scope: string;
  name: string;
  path: string;
}

export function parseSelector(text: string): Selector | null {
  const match = SELECTOR.exec(text.trim());
  if (!match?.groups) return null;
  return { scope: match.groups.scope!, name: match.groups.name!, path: match.groups.path ?? "" };
}

// 변수 선택자가 들어가는 config 키. 나머지 글자 값은 템플릿일 수 있는 것으로 본다.
const SELECTOR_KEYS: ReadonlySet<string> = new Set([
  "var",
  "query_var",
  "input_var",
  "output_var",
  "files_var",
]);
// 파이썬 코드는 템플릿이 아니다.
const CODE_KEYS: ReadonlySet<string> = new Set(["code"]);
const TEMPLATE_BLOCK = /(\{\{|\{%)([\s\S]*?)(\}\}|%\})/g;

export function renameSelector(text: string, from: string, to: string): string {
  const selector = parseSelector(text);
  if (!selector || selector.scope !== from) return text;
  return `${to}${text.trim().slice(from.length)}`;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// {{ }}·{% %} 안의 이름만 바꾼다. 속성 이름(x.llm_1)과 더 긴 이름(llm_10)은 그대로 둔다.
export function renameInTemplate(text: string, from: string, to: string): string {
  if (!text.includes(from)) return text;
  const name = new RegExp(`(?<![A-Za-z0-9_.])${escapeRegExp(from)}(?![A-Za-z0-9_])`, "g");
  return text.replace(
    TEMPLATE_BLOCK,
    (_match, open: string, body: string, close: string) =>
      `${open}${body.replace(name, to)}${close}`,
  );
}

function renameValue(value: unknown, key: string | null, from: string, to: string): unknown {
  if (typeof value === "string") {
    if (key !== null && SELECTOR_KEYS.has(key)) return renameSelector(value, from, to);
    if (key !== null && CODE_KEYS.has(key)) return value;
    return renameInTemplate(value, from, to);
  }
  if (Array.isArray(value)) {
    // 변수 집계의 vars처럼 선택자만 담은 배열은 부모 키를 이어받는다.
    const itemKey = key === "vars" ? "var" : key;
    let changed = false;
    const next = value.map((item) => {
      const renamed = renameValue(item, itemKey, from, to);
      if (renamed !== item) changed = true;
      return renamed;
    });
    return changed ? next : value;
  }
  if (typeof value === "object" && value !== null) {
    let changed = false;
    const next: Record<string, unknown> = {};
    for (const [childKey, child] of Object.entries(value)) {
      const renamed = renameValue(child, childKey, from, to);
      if (renamed !== child) changed = true;
      next[childKey] = renamed;
    }
    return changed ? next : value;
  }
  return value;
}

export function renameReferences(
  config: Record<string, unknown>,
  from: string,
  to: string,
): Record<string, unknown> {
  return renameValue(config, null, from, to) as Record<string, unknown>;
}

// id를 바꾸고 엣지·하위 노드·다른 노드의 변수 참조(선택자와 템플릿)를 함께 고친다(설계 2.2, 11.1 Q11).
export function renameNode(graph: FlowGraph, from: string, to: string): FlowGraph {
  if (from === to) return graph;
  return {
    ...graph,
    nodes: graph.nodes.map((node) => {
      const config = renameReferences(node.config, from, to);
      const id = node.id === from ? to : node.id;
      const parent = node.parent_id === from ? to : node.parent_id;
      if (config === node.config && id === node.id && parent === node.parent_id) return node;
      return { ...node, id, parent_id: parent, config };
    }),
    edges: graph.edges.map((edge) =>
      edge.source === from || edge.target === from
        ? {
            ...edge,
            source: edge.source === from ? to : edge.source,
            target: edge.target === from ? to : edge.target,
          }
        : edge,
    ),
  };
}

// 지울 노드에 iteration이 있으면 그 하위 노드도 함께 지운다. iteration_start만 따로 지우지 않는다.
export function expandRemoval(graph: FlowGraph, nodeIds: Iterable<string>): Set<string> {
  const requested = new Set(nodeIds);
  const removed = new Set<string>();
  for (const node of graph.nodes) {
    if (!requested.has(node.id)) continue;
    if (node.type === ITERATION_START_TYPE && !(node.parent_id && requested.has(node.parent_id))) {
      continue;
    }
    removed.add(node.id);
  }
  for (const node of graph.nodes) {
    if (node.parent_id && removed.has(node.parent_id)) removed.add(node.id);
  }
  return removed;
}

export function removeElements(
  graph: FlowGraph,
  nodeIds: Iterable<string>,
  edgeIds: Iterable<string> = [],
): FlowGraph {
  const nodes = expandRemoval(graph, nodeIds);
  const edges = new Set(edgeIds);
  const keptNodes = graph.nodes.filter((node) => !nodes.has(node.id));
  const keptEdges = graph.edges.filter(
    (edge) => !edges.has(edge.id) && !nodes.has(edge.source) && !nodes.has(edge.target),
  );
  if (keptNodes.length === graph.nodes.length && keptEdges.length === graph.edges.length) {
    return graph;
  }
  return { ...graph, nodes: keptNodes, edges: keptEdges };
}

// 케이스·클래스를 지우거나 오류 분기를 끄면 그 핸들에서 나가던 엣지도 지운다.
export function pruneHandleEdges(graph: FlowGraph, nodeId: string): FlowGraph {
  const node = nodeById(graph, nodeId);
  if (!node) return graph;
  const handles = new Set(sourceHandles(node));
  const edges = graph.edges.filter(
    (edge) => edge.source !== nodeId || handles.has(edge.source_handle),
  );
  return edges.length === graph.edges.length ? graph : { ...graph, edges };
}

export interface VarOption {
  selector: string;
  name: string;
  type: VarType;
}

// 서버 레지스트리의 고정 출력(apps/api/app/workflow/nodes/*.py). 노드 타입 목록을 받기 전에도 선택기가 쓸 수 있게 둔다.
const FIXED_OUTPUTS: Record<string, [string, VarType][]> = {
  answer: [["text", "string"]],
  template: [["output", "string"]],
  llm: [
    ["text", "string"],
    ["json", "object"],
    ["usage", "object"],
  ],
  http_request: [
    ["status_code", "number"],
    ["headers", "object"],
    ["files", "array[file]"],
    ["body", "string"],
    ["json", "object"],
  ],
  question_classifier: [
    ["class_name", "string"],
    ["class_label", "string"],
    ["class_id", "string"],
  ],
  agent: [
    ["text", "string"],
    ["result", "object"],
    ["files", "array[file]"],
    ["status", "string"],
    ["usage", "object"],
    ["child_run_id", "string"],
  ],
  iteration: [
    ["output", "array[any]"],
    ["count", "number"],
    ["failed_count", "number"],
  ],
  iteration_start: [
    ["item", "any"],
    ["index", "number"],
  ],
};

const START_FIELD_TYPES: Record<string, VarType> = {
  string: "string",
  paragraph: "string",
  select: "string",
  number: "number",
  boolean: "boolean",
  file: "file",
  file_list: "array[file]",
};

const VAR_TYPE_SET: ReadonlySet<string> = new Set([
  "string",
  "number",
  "boolean",
  "object",
  "file",
  "array[string]",
  "array[number]",
  "array[boolean]",
  "array[object]",
  "array[file]",
  "array[any]",
  "any",
]);

function varType(value: unknown, fallback: VarType = "any"): VarType {
  return typeof value === "string" && VAR_TYPE_SET.has(value) ? (value as VarType) : fallback;
}

function namedItems(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

// 노드 출력(이름·타입). config에 따라 달라지는 타입은 서버 outputs(config)와 같은 규칙으로 계산한다.
export function nodeOutputs(node: FlowNode, info?: NodeTypeInfo): VarOption[] {
  const make = (name: string, type: VarType): VarOption => ({
    selector: `${node.id}.${name}`,
    name,
    type,
  });
  const named = (
    items: Record<string, unknown>[],
    typeOf: (item: Record<string, unknown>) => VarType,
  ) =>
    items.flatMap((item) =>
      typeof item.name === "string" && item.name ? [make(item.name, typeOf(item))] : [],
    );
  switch (node.type) {
    case START_TYPE:
      return named(
        namedItems(node.config.fields),
        (item) => START_FIELD_TYPES[String(item.type ?? "string")] ?? "string",
      );
    case "code":
      return named(namedItems(node.config.outputs), (item) => varType(item.type));
    case "parameter_extractor":
      return [
        ...named(namedItems(node.config.parameters), (item) => varType(item.type, "string")),
        make("__is_success", "boolean"),
        make("__reason", "string"),
      ];
    case "variable_aggregator":
      return [make("output", varType(node.config.output_type))];
    default:
      break;
  }
  if (info && !info.dynamic_outputs) {
    return info.outputs.map((output) => make(output.name, output.type));
  }
  return (FIXED_OUTPUTS[node.type] ?? []).map(([name, type]) => make(name, type));
}

const WORKFLOW_SYS: [string, VarType][] = [
  ["workflow_id", "string"],
  ["run_id", "string"],
  ["kind", "string"],
  ["now", "string"],
];
const CHATFLOW_SYS: [string, VarType][] = [
  ...WORKFLOW_SYS,
  ["query", "string"],
  ["files", "array[file]"],
  ["conversation_id", "string"],
  ["message_id", "string"],
  ["user", "string"],
  ["turn", "number"],
];

export function sysVariables(kind: WorkflowKind): VarOption[] {
  return (kind === "chatflow" ? CHATFLOW_SYS : WORKFLOW_SYS).map(([name, type]) => ({
    selector: `sys.${name}`,
    name,
    type,
  }));
}

export interface ReferencedVar {
  // "<scope>.<name>". 하위 경로(.a[0])는 떼고 변수 하나로 본다.
  selector: string;
  scope: string;
  name: string;
  type: VarType | null;
}

const TEMPLATE_NAME = /(?<![A-Za-z0-9_.])([a-z][a-z0-9_]{0,31})\.([A-Za-z_][A-Za-z0-9_]*)/g;

function collectReferences(
  value: unknown,
  key: string | null,
  known: (scope: string) => boolean,
  found: Map<string, Selector>,
): void {
  if (typeof value === "string") {
    if (key !== null && SELECTOR_KEYS.has(key)) {
      const selector = parseSelector(value);
      if (selector && known(selector.scope)) {
        found.set(`${selector.scope}.${selector.name}`, { ...selector, path: "" });
      }
      return;
    }
    if (key !== null && CODE_KEYS.has(key)) return;
    for (const block of value.matchAll(TEMPLATE_BLOCK)) {
      for (const match of (block[2] ?? "").matchAll(TEMPLATE_NAME)) {
        const scope = match[1]!;
        const name = match[2]!;
        if (known(scope)) found.set(`${scope}.${name}`, { scope, name, path: "" });
      }
    }
    return;
  }
  if (Array.isArray(value)) {
    const itemKey = key === "vars" ? "var" : key;
    for (const item of value) collectReferences(item, itemKey, known, found);
    return;
  }
  if (typeof value === "object" && value !== null) {
    for (const [childKey, child] of Object.entries(value)) {
      collectReferences(child, childKey, known, found);
    }
  }
}

/*
 * 노드가 참조하는 변수(단일 노드 실행의 입력칸, 설계 6.1·7.3). 선택자 칸과 템플릿 안의 이름을 모은다. env는
 * 서버가 초안 값을 쓰고, 실행할 노드 자신(iteration이면 안쪽 노드까지)의 출력은 줄 수 없어 뺀다(서버
 * workflow_single_run과 같은 규칙). iteration은 안쪽 노드가 바깥을 가리키는 것도 넣는다.
 */
export function referencedVariables(
  graph: FlowGraph,
  nodeId: string,
  kind: WorkflowKind,
  options: {
    nodeTypes?: NodeTypeInfo[];
    conversationVars?: { name: string; type: VarType }[];
  } = {},
): ReferencedVar[] {
  const node = nodeById(graph, nodeId);
  if (!node) return [];
  const inner = graph.nodes.filter((item) => item.parent_id === nodeId);
  const own = new Set([nodeId, ...inner.map((item) => item.id)]);
  const nodes = new Map(graph.nodes.map((item) => [item.id, item]));
  const known = (scope: string) =>
    !own.has(scope) &&
    (scope === "sys" || (scope === "conv" && kind === "chatflow") || nodes.has(scope));
  const found = new Map<string, Selector>();
  for (const source of [node, ...inner]) collectReferences(source.config, null, known, found);

  const infos = new Map((options.nodeTypes ?? []).map((info) => [info.type, info]));
  const sys = new Map(sysVariables(kind).map((item) => [item.name, item.type]));
  const conv = new Map((options.conversationVars ?? []).map((item) => [item.name, item.type]));
  const typeOf = (selector: Selector): VarType | null => {
    if (selector.scope === "sys") return sys.get(selector.name) ?? null;
    if (selector.scope === "conv") return conv.get(selector.name) ?? null;
    const target = nodes.get(selector.scope);
    if (!target) return null;
    const output = nodeOutputs(target, infos.get(target.type)).find(
      (item) => item.name === selector.name,
    );
    return output?.type ?? null;
  };
  return [...found.entries()].map(([key, selector]) => ({
    selector: key,
    scope: selector.scope,
    name: selector.name,
    type: typeOf(selector),
  }));
}

// 서버 Issue.field("cases[0].conditions[1].var")를 폼 경로("cases.0.conditions.1.var")로.
export function issuePath(field: string): string {
  return field.replace(/\[(\d+)\]/g, ".$1");
}

const NODE_LEVEL_FIELDS = ["id", "type", "title", "parent_id"];

// 노드 바깥 칸(id, error.*, retry.*)의 이슈인지. 나머지는 config 안의 경로다.
export function isNodeLevelField(field: string): boolean {
  return (
    NODE_LEVEL_FIELDS.includes(field) || field.startsWith("error.") || field.startsWith("retry.")
  );
}

export interface IssueCount {
  errors: number;
  warnings: number;
}

export function countIssues(issues: WfIssue[]): Map<string, IssueCount> {
  const counts = new Map<string, IssueCount>();
  for (const issue of issues) {
    const key = issue.node_id ?? (issue.edge_id ? `edge:${issue.edge_id}` : "");
    if (!key) continue;
    const count = counts.get(key) ?? { errors: 0, warnings: 0 };
    if (issue.level === "error") count.errors += 1;
    else count.warnings += 1;
    counts.set(key, count);
  }
  return counts;
}

// 서버가 받는 그래프 모양. 노드는 이미 정규화되어 있어 그대로 보낸다.
export function toGraphPayload(graph: FlowGraph): WfGraphOut {
  return {
    nodes: graph.nodes.map((node) => ({ ...node })),
    edges: graph.edges.map((edge) => ({ ...edge })),
    viewport: graph.viewport,
  };
}

// ReactFlow 하위 흐름은 부모 노드가 자식보다 먼저 와야 한다.
export function parentsFirst(nodes: FlowNode[]): FlowNode[] {
  if (!nodes.some((node) => node.parent_id)) return nodes;
  return [...nodes.filter((node) => !node.parent_id), ...nodes.filter((node) => node.parent_id)];
}

// 서버 variables.assignable과 같은 규칙. no면 검증이 type_mismatch 경고를 낸다.
export function assignable(source: VarType | null, target: VarType | null): "yes" | "maybe" | "no" {
  if (source === null || target === null || source === target) return "yes";
  if (source === "any" || target === "any" || target === "array[any]") return "yes";
  if (target === "string" && (source === "number" || source === "boolean")) return "yes";
  if (target === "object" && source === "file") return "yes";
  if (target === "number" && source === "string") return "maybe";
  if (source === "array[any]" && target.startsWith("array[")) return "maybe";
  return "no";
}

const DEFAULT_SIZE = { width: 240, height: 90 };

// 새 노드를 같은 프레임의 다른 노드와 겹치지 않는 자리에 둔다. 겹치면 아래로 내린다.
export function freePosition(
  graph: FlowGraph,
  wanted: { x: number; y: number },
  parentId: string | null,
  sizes: Record<string, { width: number; height: number }> = {},
): { x: number; y: number } {
  const others = graph.nodes
    .filter((node) => node.parent_id === parentId)
    .map((node) => ({ ...node.position, ...(sizes[node.id] ?? DEFAULT_SIZE) }));
  const position = { ...wanted };
  const overlaps = () =>
    others.some(
      (rect) =>
        position.x < rect.x + rect.width &&
        position.x + DEFAULT_SIZE.width > rect.x &&
        position.y < rect.y + rect.height &&
        position.y + DEFAULT_SIZE.height > rect.y,
    );
  for (let tries = 0; tries < 50 && overlaps(); tries += 1) {
    position.y += DEFAULT_SIZE.height + 30;
  }
  return position;
}
