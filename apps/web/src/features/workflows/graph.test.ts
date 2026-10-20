import { describe, expect, it } from "vitest";
import type { NodeTypeInfo, WorkflowKind } from "@/api/types";
import {
  connectProblem,
  enclosingLoop,
  expandRemoval,
  freePosition,
  hasTargetHandle,
  loopVariables,
  nextEdgeId,
  nextNodeId,
  nodeById,
  nodeIdProblem,
  nodeOutputs,
  normalizeGraph,
  pruneHandleEdges,
  referencedVariables,
  renameInTemplate,
  renameNode,
  renameSelector,
  sourceHandles,
  upstreamIds,
  type FlowGraph,
  type FlowNode,
} from "./graph";
import { createNodes, loopOnly, paletteTypes } from "./nodeTypes";

function node(id: string, type: string, extra: Partial<FlowNode> = {}): FlowNode {
  return {
    id,
    type,
    version: 1,
    title: id,
    config: {},
    error: { strategy: "fail", default_outputs: {} },
    retry: { max_attempts: 0, interval_ms: 1000 },
    parent_id: null,
    position: { x: 0, y: 0 },
    ui: {},
    ...extra,
  };
}

function edge(source: string, target: string, handle = "out") {
  return {
    id: `${source}-${handle}-${target}`,
    source,
    source_handle: handle,
    target,
    target_handle: "in",
  };
}

// start → llm_1 → if_else_1 →(case_a) end_1, iteration_1(하위: iteration_start_1 → code_1)
function sample(): FlowGraph {
  return {
    nodes: [
      node("start", "start", { config: { fields: [{ name: "q", type: "string" }] } }),
      node("llm_1", "llm"),
      node("if_else_1", "if_else", { config: { cases: [{ id: "a", conditions: [] }] } }),
      node("end_1", "end"),
      node("iteration_1", "iteration"),
      node("iteration_start_1", "iteration_start", { parent_id: "iteration_1" }),
      node("code_1", "code", {
        parent_id: "iteration_1",
        config: { outputs: [{ name: "r", type: "number" }] },
      }),
    ],
    edges: [
      edge("start", "llm_1"),
      edge("llm_1", "if_else_1"),
      edge("if_else_1", "end_1", "case_a"),
      edge("llm_1", "iteration_1"),
      edge("iteration_start_1", "code_1"),
    ],
    viewport: {},
  };
}

describe("normalizeGraph", () => {
  it("빠진 기본값을 채운다", () => {
    const graph = normalizeGraph({
      nodes: [{ id: "a", type: "start", version: 1, title: "" }],
      edges: [{ id: "e", source: "a", source_handle: "", target: "b", target_handle: "" }],
    });
    expect(graph.nodes[0]).toMatchObject({
      config: {},
      error: { strategy: "fail", default_outputs: {} },
      retry: { max_attempts: 0, interval_ms: 1000 },
      parent_id: null,
      position: { x: 0, y: 0 },
    });
    expect(graph.edges[0]).toMatchObject({ source_handle: "out", target_handle: "in" });
  });
});

describe("upstreamIds", () => {
  it("같은 프레임의 조상을 가까운 것부터", () => {
    expect(upstreamIds(sample(), "end_1")).toEqual(["if_else_1", "llm_1", "start"]);
    expect(upstreamIds(sample(), "start")).toEqual([]);
  });

  it("반복 안 노드는 반복 노드의 바깥 조상도 본다", () => {
    expect(upstreamIds(sample(), "code_1")).toEqual(["iteration_start_1", "llm_1", "start"]);
  });

  it("다른 프레임을 넘는 엣지는 상류로 치지 않는다", () => {
    const graph = sample();
    graph.edges.push(edge("end_1", "code_1"));
    expect(upstreamIds(graph, "code_1")).toEqual(["iteration_start_1", "llm_1", "start"]);
  });
});

describe("sourceHandles", () => {
  it("분기 노드는 config에서 핸들을 만들고 branch면 fail을 더한다", () => {
    const graph = sample();
    expect(sourceHandles(graph.nodes[2]!)).toEqual(["case_a", "else"]);
    expect(sourceHandles(node("end_9", "end"))).toEqual([]);
    expect(
      sourceHandles(
        node("q", "question_classifier", { config: { classes: [{ id: "1" }, { id: "2" }] } }),
      ),
    ).toEqual(["class_1", "class_2"]);
    expect(
      sourceHandles(
        node("h", "http_request", { error: { strategy: "branch", default_outputs: {} } }),
      ),
    ).toEqual(["out", "fail"]);
  });
});

describe("connectProblem", () => {
  it("서버가 오류로 보는 연결을 막는다", () => {
    const graph = sample();
    expect(connectProblem(graph, { source: "llm_1", target: "llm_1" })).toBe("self");
    expect(connectProblem(graph, { source: "llm_1", target: "start" })).toBe("entry");
    expect(connectProblem(graph, { source: "llm_1", target: "code_1" })).toBe("frame");
    expect(connectProblem(graph, { source: "llm_1", sourceHandle: "fail", target: "end_1" })).toBe(
      "handle",
    );
    expect(connectProblem(graph, { source: "start", target: "llm_1" })).toBe("duplicate");
    expect(
      connectProblem(graph, { source: "if_else_1", sourceHandle: "else", target: "llm_1" }),
    ).toBe("cycle");
    expect(connectProblem(graph, { source: "start", target: "end_1" })).toBeNull();
    expect(
      connectProblem(graph, { source: "if_else_1", sourceHandle: "else", target: "end_1" }),
    ).toBeNull();
  });
});

describe("id", () => {
  it("<type>_<n>으로 만들고 start는 하나다", () => {
    const graph = sample();
    expect(nextNodeId(graph, "llm")).toBe("llm_2");
    expect(nextNodeId(graph, "template")).toBe("template_1");
    expect(nextNodeId({ ...graph, nodes: [] }, "start")).toBe("start");
    expect(nextNodeId(graph, "start")).toBe("start_1");
    expect(nextEdgeId({ nodes: [], edges: [{ ...edge("a", "b"), id: "e7" }], viewport: {} })).toBe(
      "e8",
    );
  });

  it("바꿀 id를 검사한다", () => {
    const graph = sample();
    expect(nodeIdProblem(graph, "llm_1", "llm_1")).toBeNull();
    expect(nodeIdProblem(graph, "llm_1", "Writer")).toBe("pattern");
    expect(nodeIdProblem(graph, "llm_1", "1x")).toBe("pattern");
    expect(nodeIdProblem(graph, "llm_1", "env")).toBe("reserved");
    expect(nodeIdProblem(graph, "llm_1", "end_1")).toBe("taken");
    expect(nodeIdProblem(graph, "llm_1", "writer")).toBeNull();
  });
});

describe("참조 치환", () => {
  it("선택자는 범위가 같을 때만 바꾼다", () => {
    expect(renameSelector("llm_1.text", "llm_1", "writer")).toBe("writer.text");
    expect(renameSelector("llm_1.json.items[0]", "llm_1", "writer")).toBe("writer.json.items[0]");
    expect(renameSelector("llm_10.text", "llm_1", "writer")).toBe("llm_10.text");
    expect(renameSelector("sys.query", "llm_1", "writer")).toBe("sys.query");
  });

  it("템플릿은 {{ }}·{% %} 안의 이름만 바꾼다", () => {
    const text =
      "llm_1 그대로 {{ llm_1.text }} {{llm_10.text}} {{ x.llm_1 }} {% for i in llm_1.json %}{{ i }}{% endfor %}";
    expect(renameInTemplate(text, "llm_1", "writer")).toBe(
      "llm_1 그대로 {{ writer.text }} {{llm_10.text}} {{ x.llm_1 }} {% for i in writer.json %}{{ i }}{% endfor %}",
    );
  });

  it("노드 id를 바꾸면 엣지·하위 노드·다른 노드의 참조를 함께 고친다", () => {
    const graph = sample();
    graph.nodes.push(
      node("end_2", "end", { config: { outputs: [{ name: "o", var: "llm_1.text" }] } }),
      node("tpl", "template", {
        config: { template: "{{ v }}", vars: [{ name: "v", var: "llm_1.json.a" }] },
      }),
      node("agg", "variable_aggregator", { config: { vars: ["llm_1.text", "start.q"] } }),
      node("ans", "answer", { config: { text: "답: {{ llm_1.text }}" } }),
      node("py", "code", { config: { code: "x = '{{ llm_1.text }}'" } }),
    );
    const renamed = renameNode(graph, "llm_1", "writer");
    const byId = new Map(renamed.nodes.map((item) => [item.id, item]));
    expect(byId.has("llm_1")).toBe(false);
    expect(byId.get("writer")?.type).toBe("llm");
    expect(byId.get("end_2")?.config).toEqual({ outputs: [{ name: "o", var: "writer.text" }] });
    expect(byId.get("tpl")?.config.vars).toEqual([{ name: "v", var: "writer.json.a" }]);
    expect(byId.get("agg")?.config.vars).toEqual(["writer.text", "start.q"]);
    expect(byId.get("ans")?.config.text).toBe("답: {{ writer.text }}");
    // 파이썬 코드는 템플릿이 아니다.
    expect(byId.get("py")?.config.code).toBe("x = '{{ llm_1.text }}'");
    expect(renamed.edges.filter((item) => item.source === "writer").length).toBe(2);
    expect(renamed.edges.some((item) => item.target === "writer")).toBe(true);
    // 바뀌지 않은 노드는 같은 객체다(카드가 다시 그려지지 않는다).
    expect(byId.get("code_1")).toBe(graph.nodes.find((item) => item.id === "code_1"));

    const parent = renameNode(sample(), "iteration_1", "loop");
    expect(parent.nodes.filter((item) => item.parent_id === "loop").map((item) => item.id)).toEqual(
      ["iteration_start_1", "code_1"],
    );
  });
});

describe("지우기", () => {
  it("반복 노드를 지우면 하위 노드도 지우고 iteration_start만 따로는 지우지 않는다", () => {
    const graph = sample();
    expect([...expandRemoval(graph, ["iteration_1"])].sort()).toEqual([
      "code_1",
      "iteration_1",
      "iteration_start_1",
    ]);
    expect([...expandRemoval(graph, ["iteration_start_1"])]).toEqual([]);
  });

  it("사라진 분기 핸들의 엣지를 지운다", () => {
    const graph = sample();
    graph.nodes[2] = { ...graph.nodes[2]!, config: { cases: [{ id: "b", conditions: [] }] } };
    const pruned = pruneHandleEdges(graph, "if_else_1");
    expect(pruned.edges.some((item) => item.source_handle === "case_a")).toBe(false);
    expect(pruned.edges).toHaveLength(graph.edges.length - 1);
    expect(pruneHandleEdges(sample(), "if_else_1").edges).toHaveLength(graph.edges.length);
  });
});

describe("nodeOutputs", () => {
  it("config에 따라 달라지는 출력을 서버와 같은 규칙으로 계산한다", () => {
    const graph = sample();
    expect(nodeOutputs(graph.nodes[0]!).map((item) => `${item.selector}:${item.type}`)).toEqual([
      "start.q:string",
    ]);
    expect(nodeOutputs(graph.nodes[6]!).map((item) => item.selector)).toEqual(["code_1.r"]);
    expect(
      nodeOutputs(
        node("pe", "parameter_extractor", { config: { parameters: [{ name: "city" }] } }),
      ).map((item) => `${item.name}:${item.type}`),
    ).toEqual(["city:string", "__is_success:boolean", "__reason:string"]);
    expect(
      nodeOutputs(node("va", "variable_aggregator", { config: { output_type: "number" } })),
    ).toEqual([{ selector: "va.output", name: "output", type: "number" }]);
    expect(nodeOutputs(graph.nodes[1]!).map((item) => item.name)).toEqual([
      "text",
      "json",
      "usage",
    ]);
  });
});

describe("freePosition", () => {
  it("겹치면 아래로 내린다", () => {
    const graph: FlowGraph = {
      nodes: [node("a", "llm", { position: { x: 0, y: 0 } })],
      edges: [],
      viewport: {},
    };
    expect(freePosition(graph, { x: 100, y: 10 }, null)).toEqual({ x: 100, y: 130 });
    expect(freePosition(graph, { x: 400, y: 0 }, null)).toEqual({ x: 400, y: 0 });
    expect(freePosition(graph, { x: 100, y: 10 }, "other")).toEqual({ x: 100, y: 10 });
  });
});

describe("referencedVariables", () => {
  it("선택자 칸과 템플릿 안의 이름을 하위 경로 없이 모으고 env와 모르는 이름은 뺀다", () => {
    const graph = sample();
    graph.nodes[1] = node("llm_1", "llm", {
      config: {
        prompt: [
          {
            role: "system",
            text: "{{ env.KEY }} 규칙. {% if sys.query %}{{ sys.query }}{% endif %}",
          },
          { role: "user", text: "{{ start.q }} / {{ start.q.upper() }} / {{ item.name }} {{ x }}" },
        ],
        vision: { enabled: true, var: "start.q[0]" },
      },
    });
    expect(referencedVariables(graph, "llm_1", "chatflow")).toEqual([
      { selector: "sys.query", scope: "sys", name: "query", type: "string" },
      { selector: "start.q", scope: "start", name: "q", type: "string" },
    ]);
    // 워크플로우에는 sys.query가 없어 타입을 모른다.
    expect(referencedVariables(graph, "llm_1", "workflow")[0]).toMatchObject({
      selector: "sys.query",
      type: null,
    });
  });

  it("iteration은 안쪽 노드가 바깥을 가리키는 것까지 넣고 자기 프레임 안의 출력은 뺀다", () => {
    const graph = sample();
    graph.nodes[4] = node("iteration_1", "iteration", {
      config: { input_var: "llm_1.json.items", output_var: "code_1.r" },
    });
    graph.nodes[6] = node("code_1", "code", {
      parent_id: "iteration_1",
      config: {
        code: "def main(item): return {'r': '{{ start.q }}'}",
        inputs: [
          { name: "item", var: "iteration_start_1.item" },
          { name: "q", var: "start.q" },
        ],
        outputs: [{ name: "r", type: "number" }],
      },
    });
    expect(referencedVariables(graph, "iteration_1", "workflow").map((v) => v.selector)).toEqual([
      "llm_1.json",
      "start.q",
    ]);
    // 안쪽 노드 하나만 돌릴 때는 iteration_start의 item도 입력이다.
    expect(referencedVariables(graph, "code_1", "workflow")).toEqual([
      { selector: "iteration_start_1.item", scope: "iteration_start_1", name: "item", type: "any" },
      { selector: "start.q", scope: "start", name: "q", type: "string" },
    ]);
  });

  it("변수 모으기의 후보 배열과 대화 변수 타입을 읽는다", () => {
    const graph = sample();
    graph.nodes.push(
      node("agg_1", "variable_aggregator", {
        config: { vars: ["llm_1.text", "code_1.r"], output_type: "string" },
      }),
      node("assign_1", "variable_assigner", {
        config: { items: [{ target: "conv.topic", op: "overwrite", var: "conv.count" }] },
      }),
    );
    expect(referencedVariables(graph, "agg_1", "workflow").map((v) => v.selector)).toEqual([
      "llm_1.text",
      "code_1.r",
    ]);
    expect(
      referencedVariables(graph, "assign_1", "chatflow", {
        conversationVars: [{ name: "count", type: "number" }],
      }),
    ).toEqual([{ selector: "conv.count", scope: "conv", name: "count", type: "number" }]);
    expect(referencedVariables(graph, "nope", "workflow")).toEqual([]);
  });
});

// start → list_1 → loop_1(하위: loop_start_1 → assign_1 → tool_1) → ask_1
function loopSample(): FlowGraph {
  return {
    nodes: [
      node("start", "start", { config: { fields: [{ name: "q", type: "string" }] } }),
      node("list_1", "list_operator", { config: { input_var: "start.q" } }),
      node("loop_1", "loop", {
        config: {
          loop_vars: [
            { name: "n", type: "number", value: 0 },
            { name: "text", type: "string", var: "list_1.first_record" },
          ],
          break_conditions: [{ var: "loop_1.n", op: "≥", value: "3" }],
          logical: "and",
          max_iterations: 5,
        },
      }),
      node("loop_start_1", "loop_start", { parent_id: "loop_1" }),
      node("assign_1", "variable_assigner", {
        parent_id: "loop_1",
        config: { items: [{ target: "loop_1.n", op: "+=", value: 1 }] },
      }),
      node("tool_1", "tool", {
        parent_id: "loop_1",
        config: {
          tool: "json_parse",
          params: {
            text: { mode: "var", value: "loop_1.text" },
            path: { mode: "const", value: "{{ loop_1.text }}" },
          },
        },
      }),
      node("ask_1", "human_input", {
        config: {
          description: "{{ loop_1.text }}",
          fields: [
            { name: "ok", type: "boolean" },
            { name: "docs", type: "file_list" },
          ],
        },
      }),
    ],
    edges: [
      edge("start", "list_1"),
      edge("list_1", "loop_1"),
      edge("loop_start_1", "assign_1"),
      edge("assign_1", "tool_1"),
      edge("loop_1", "ask_1"),
    ],
    viewport: {},
  };
}

const jsonParse = {
  name: "json_parse",
  label: "JSON 읽기",
  description: "",
  params: [],
  params_schema: {},
  outputs: [
    { name: "value", type: "any" as const },
    { name: "found", type: "boolean" as const },
  ],
};

describe("loop 하위 그래프", () => {
  it("틀을 만들면 loop_start가 함께 생기고 진입 자리라 들어오는 연결을 받지 않는다", () => {
    const graph = loopSample();
    const nodes = createNodes(graph, {
      type: "loop",
      kind: "workflow",
      title: "루프",
      position: { x: 0, y: 0 },
      labels: { className: (n) => `분류 ${n}` },
      entryTitle: "루프 시작",
    });
    expect(nodes.map((item) => [item.id, item.type, item.parent_id])).toEqual([
      ["loop_2", "loop", null],
      ["loop_start_2", "loop_start", "loop_2"],
    ]);
    expect(nodes[0]!.config).toMatchObject({ loop_vars: [], max_iterations: 10 });
    expect(nodes[0]!.ui).toEqual({ width: 520, height: 260 });
    expect(hasTargetHandle(nodes[1]!)).toBe(false);
    expect(connectProblem(graph, { source: "assign_1", target: "loop_start_1" })).toBe("entry");
    expect(connectProblem(graph, { source: "list_1", target: "assign_1" })).toBe("frame");
  });

  it("안쪽 노드는 loop의 바깥 상류와 반복 변수를 본다", () => {
    const graph = loopSample();
    expect(upstreamIds(graph, "tool_1")).toEqual(["assign_1", "loop_start_1", "list_1", "start"]);
    expect(enclosingLoop(graph, "tool_1")?.id).toBe("loop_1");
    expect(enclosingLoop(graph, "ask_1")).toBeUndefined();
    expect(loopVariables(nodeById(graph, "loop_1")!)).toEqual([
      { selector: "loop_1.n", name: "n", type: "number" },
      { selector: "loop_1.text", name: "text", type: "string" },
    ]);
  });

  it("출력: loop는 반복 변수와 loop_count, human_input은 필드, tool은 고른 도구의 출력", () => {
    const graph = loopSample();
    const names = (id: string, tools = [jsonParse]) =>
      nodeOutputs(nodeById(graph, id)!, undefined, tools).map(
        (item) => `${item.name}:${item.type}`,
      );
    expect(names("loop_1")).toEqual(["n:number", "text:string", "loop_count:number"]);
    expect(names("loop_start_1")).toEqual(["index:number"]);
    expect(names("ask_1")).toEqual(["ok:boolean", "docs:array[file]"]);
    expect(names("tool_1")).toEqual(["value:any", "found:boolean"]);
    expect(names("tool_1", [])).toEqual([]);
    expect(names("list_1")).toEqual([
      "result:array[any]",
      "count:number",
      "first_record:any",
      "last_record:any",
      "item:any",
    ]);
  });

  it("loop를 지우면 안쪽까지 지우고 loop_start만 따로는 지우지 않는다", () => {
    const graph = loopSample();
    expect([...expandRemoval(graph, ["loop_start_1"])]).toEqual([]);
    expect([...expandRemoval(graph, ["loop_1"])].sort()).toEqual([
      "assign_1",
      "loop_1",
      "loop_start_1",
      "tool_1",
    ]);
  });

  it("loop id를 바꾸면 대입 대상·도구 변수 매개변수·템플릿을 고치고 고정값은 그대로 둔다", () => {
    const renamed = renameNode(loopSample(), "loop_1", "count");
    const config = (id: string) => nodeById(renamed, id)!.config;
    expect(config("assign_1").items).toEqual([{ target: "count.n", op: "+=", value: 1 }]);
    expect(config("tool_1").params).toEqual({
      text: { mode: "var", value: "count.text" },
      path: { mode: "const", value: "{{ loop_1.text }}" },
    });
    expect(config("count").break_conditions).toEqual([{ var: "count.n", op: "≥", value: "3" }]);
    expect(config("ask_1").description).toBe("{{ count.text }}");
    expect(nodeById(renamed, "loop_start_1")!.parent_id).toBe("count");
  });

  it("노드 하나 실행의 입력칸은 도구 변수 매개변수를 넣고 고정값 글은 템플릿으로 읽지 않는다", () => {
    const graph = loopSample();
    const refs = referencedVariables(graph, "tool_1", "workflow", { tools: [jsonParse] });
    expect(refs.map((item) => [item.selector, item.type])).toEqual([["loop_1.text", "string"]]);
    // +=는 대상의 지금 값을 읽으므로 입력칸이 된다(덮어쓰기는 읽지 않는다).
    expect(referencedVariables(graph, "assign_1", "workflow").map((v) => v.selector)).toEqual([
      "loop_1.n",
    ]);
    // loop 자신을 돌리면 안쪽이 읽는 반복 변수는 루프가 만든다.
    const own = referencedVariables(graph, "loop_1", "workflow");
    expect(own.map((item) => item.selector)).toEqual(["list_1.first_record"]);
  });
});

describe("팔레트", () => {
  const info = (type: string, kinds: WorkflowKind[], loopKinds: WorkflowKind[] = []) =>
    ({ type, kinds, loop_kinds: loopKinds }) as unknown as NodeTypeInfo;

  it("워크플로우는 loop 안에서만 되는 노드도 보이고 진입 자리는 숨긴다", () => {
    const infos = [
      info("start", ["workflow", "chatflow"]),
      info("loop", ["workflow", "chatflow"]),
      info("loop_start", ["workflow", "chatflow"]),
      info("variable_assigner", ["chatflow"], ["chatflow", "workflow"]),
      info("answer", ["chatflow"]),
    ];
    expect(paletteTypes(infos, "workflow")).toEqual(["start", "loop", "variable_assigner"]);
    expect(loopOnly(infos[3], "workflow")).toBe(true);
    expect(loopOnly(infos[3], "chatflow")).toBe(false);
    expect(loopOnly(infos[4], "workflow")).toBe(false);
  });
});
