import { describe, expect, it } from "vitest";
import type { WorkflowVersionOut } from "@/api/types";
import { COALESCE_MS, createFlowStore, HISTORY_LIMIT, isDirty } from "./flowStore";
import { draftPayload } from "./useDraftAutosave";

function draft(): WorkflowVersionOut {
  return {
    id: "v0",
    workflow_id: "w1",
    kind: "workflow",
    version_number: null,
    status: "draft",
    graph: {
      nodes: [
        {
          id: "start",
          type: "start",
          version: 1,
          title: "시작",
          config: { fields: [{ name: "q", type: "string" }] },
          position: { x: 0, y: 0 },
        },
        {
          id: "llm_1",
          type: "llm",
          version: 1,
          title: "LLM",
          config: { prompt: [{ role: "user", text: "{{ start.q }}" }] },
          position: { x: 300, y: 0 },
        },
        {
          id: "if_else_1",
          type: "if_else",
          version: 1,
          title: "분기",
          config: {
            cases: [
              { id: "1", logical: "and", conditions: [{ var: "llm_1.text", op: "=", value: "" }] },
              { id: "2", logical: "and", conditions: [{ var: "llm_1.text", op: "=", value: "x" }] },
            ],
          },
          position: { x: 600, y: 0 },
        },
        {
          id: "end_1",
          type: "end",
          version: 1,
          title: "끝",
          config: { outputs: [{ name: "answer", var: "llm_1.text" }] },
          position: { x: 900, y: 0 },
        },
      ],
      edges: [
        { id: "e1", source: "start", source_handle: "out", target: "llm_1", target_handle: "in" },
        {
          id: "e2",
          source: "llm_1",
          source_handle: "out",
          target: "if_else_1",
          target_handle: "in",
        },
        {
          id: "e3",
          source: "if_else_1",
          source_handle: "case_2",
          target: "end_1",
          target_handle: "in",
        },
      ],
      viewport: {},
    },
    env_vars: [
      { name: "BASE", secret: false, value: "https://x", has_value: true },
      { name: "TOKEN", secret: true, value: null, has_value: true },
    ],
    conversation_vars: [],
    settings: { approval_mode: "inherit", mask_secrets_in_outputs: true },
    graph_hash: "h",
    published_at: null,
    change_note: null,
    issues: [
      { level: "error", code: "x", message: "m", node_id: "llm_1" },
      { level: "warning", code: "y", message: "m", edge_id: "e3" },
    ],
    created_at: "2026-10-14T00:00:00Z",
    updated_at: "2026-10-14T00:00:00Z",
  };
}

function setup() {
  let clock = 0;
  const store = createFlowStore("w1", "workflow", () => clock);
  store.getState().actions.load(draft());
  return {
    store,
    actions: store.getState().actions,
    tick: (ms: number) => {
      clock += ms;
    },
  };
}

function ids(store: ReturnType<typeof setup>["store"]) {
  return store.getState().graph.nodes.map((node) => node.id);
}

describe("flowStore", () => {
  it("불러오면 깨끗한 상태이고 이슈를 노드·엣지별로 센다", () => {
    const { store } = setup();
    const state = store.getState();
    expect(isDirty(state)).toBe(false);
    expect(state.issueCounts.get("llm_1")).toEqual({ errors: 1, warnings: 0 });
    expect(state.issueCounts.get("edge:e3")).toEqual({ errors: 0, warnings: 1 });
    expect(state.envVars).toEqual([
      { name: "BASE", value: "https://x", secret: false, keep: false },
      { name: "TOKEN", value: "", secret: true, keep: true },
    ]);
  });

  it("노드 추가·연결·삭제를 실행 취소·다시 실행한다", () => {
    const { store, actions } = setup();
    const template = {
      ...store.getState().graph.nodes[1]!,
      id: "template_1",
      type: "template",
      config: {},
    };
    actions.addNodes([template], { source: "llm_1", handle: "out" });
    expect(ids(store)).toContain("template_1");
    expect(store.getState().selection).toEqual(["template_1"]);
    expect(store.getState().graph.edges.some((edge) => edge.target === "template_1")).toBe(true);
    expect(actions.connect({ source: "template_1", target: "end_1" })).toBeNull();
    expect(actions.connect({ source: "end_1", target: "llm_1" })).toBe("handle");
    actions.remove(["llm_1"], []);
    expect(ids(store)).not.toContain("llm_1");
    // llm_1에 붙은 엣지도 사라진다.
    expect(store.getState().graph.edges.some((edge) => edge.source === "llm_1")).toBe(false);
    expect(isDirty(store.getState())).toBe(true);

    actions.undo();
    expect(ids(store)).toContain("llm_1");
    actions.undo();
    actions.undo();
    expect(ids(store)).not.toContain("template_1");
    expect(store.getState().selection).toEqual([]);
    actions.redo();
    expect(ids(store)).toContain("template_1");
    // 새 변경을 하면 다시 실행 목록은 비운다.
    actions.updateNode("start", { title: "처음" });
    expect(store.getState().future).toEqual([]);
  });

  it("실행 취소는 50단계까지 남고, 같은 칸을 이어 고치면 한 단계로 묶는다", () => {
    const { store, actions } = setup();
    for (let i = 0; i < HISTORY_LIMIT + 10; i += 1) {
      actions.updateNode("start", { title: `제목 ${i}` });
    }
    expect(store.getState().past).toHaveLength(HISTORY_LIMIT);

    const fresh = setup();
    fresh.actions.updateNode("start", { title: "가" }, "title:start");
    fresh.tick(100);
    fresh.actions.updateNode("start", { title: "가나" }, "title:start");
    fresh.tick(100);
    fresh.actions.updateNode("start", { title: "가나다" }, "title:start");
    expect(fresh.store.getState().past).toHaveLength(1);
    fresh.tick(COALESCE_MS + 1);
    fresh.actions.updateNode("start", { title: "가나다라" }, "title:start");
    expect(fresh.store.getState().past).toHaveLength(2);
    fresh.actions.undo();
    expect(fresh.store.getState().graph.nodes[0]?.title).toBe("가나다");
    fresh.actions.undo();
    expect(fresh.store.getState().graph.nodes[0]?.title).toBe("시작");
  });

  it("끌기 한 번이 실행 취소 한 번이고, 움직이지 않았으면 자리를 남기지 않는다", () => {
    const { store, actions } = setup();
    actions.beginMove();
    actions.moveNodes([{ id: "llm_1", position: { x: 310.4, y: 5 } }]);
    actions.moveNodes([{ id: "llm_1", position: { x: 340, y: 40.6 } }]);
    actions.endMove();
    expect(store.getState().past).toHaveLength(1);
    expect(store.getState().graph.nodes[1]?.position).toEqual({ x: 340, y: 41 });
    actions.beginMove();
    actions.endMove();
    expect(store.getState().past).toHaveLength(1);
    actions.undo();
    expect(store.getState().graph.nodes[1]?.position).toEqual({ x: 300, y: 0 });
  });

  it("id를 바꾸면 참조를 고치고 실행 취소로 되돌리며 폼을 새로 만들게 한다", () => {
    const { store, actions } = setup();
    actions.select(["llm_1"]);
    const epoch = store.getState().formEpoch;
    actions.rename("llm_1", "writer");
    const state = store.getState();
    expect(ids(store)).toEqual(["start", "writer", "if_else_1", "end_1"]);
    expect(state.selection).toEqual(["writer"]);
    expect(state.formEpoch).toBe(epoch + 1);
    expect(state.graph.nodes[3]?.config).toEqual({
      outputs: [{ name: "answer", var: "writer.text" }],
    });
    expect(
      (state.graph.nodes[2]?.config.cases as { conditions: { var: string }[] }[])[0]?.conditions[0]
        ?.var,
    ).toBe("writer.text");
    expect(state.graph.edges.map((edge) => `${edge.source}->${edge.target}`)).toEqual([
      "start->writer",
      "writer->if_else_1",
      "if_else_1->end_1",
    ]);
    actions.undo();
    expect(ids(store)).toContain("llm_1");
    expect(store.getState().selection).toEqual([]);
    expect(store.getState().formEpoch).toBe(epoch + 2);
  });

  it("케이스를 지우면 그 핸들의 엣지를 같은 실행 취소 단계에서 지운다", () => {
    const { store, actions } = setup();
    const ifElse = store.getState().graph.nodes[2]!;
    const cases = ifElse.config.cases as unknown[];
    actions.updateConfig("if_else_1", { cases: cases.slice(0, 1) });
    expect(store.getState().graph.edges.some((edge) => edge.source_handle === "case_2")).toBe(
      false,
    );
    actions.undo();
    expect(store.getState().graph.edges.some((edge) => edge.source_handle === "case_2")).toBe(true);
    // 같은 값이면 아무것도 하지 않는다.
    const past = store.getState().past.length;
    actions.updateConfig("if_else_1", ifElse.config);
    expect(store.getState().past).toHaveLength(past);
  });

  it("오류 분기를 끄면 fail 엣지를 지운다", () => {
    const { store, actions } = setup();
    actions.updateNode("llm_1", { error: { strategy: "branch", default_outputs: {} } });
    expect(actions.connect({ source: "llm_1", sourceHandle: "fail", target: "end_1" })).toBeNull();
    actions.updateNode("llm_1", { error: { strategy: "fail", default_outputs: {} } });
    expect(store.getState().graph.edges.some((edge) => edge.source_handle === "fail")).toBe(false);
  });

  it("환경 변수·설정은 저장할 변경으로 세지만 실행 취소에는 들어가지 않는다", () => {
    const { store, actions } = setup();
    actions.setEnvVars([{ name: "BASE", value: "https://y", secret: false, keep: false }]);
    expect(isDirty(store.getState())).toBe(true);
    expect(store.getState().past).toHaveLength(0);
    const payload = draftPayload(store.getState());
    expect(payload.env_vars).toEqual([{ name: "BASE", secret: false, value: "https://y" }]);
    expect(payload.updated_at).toBe("2026-10-14T00:00:00Z");
  });

  it("비밀 값은 비워 두면 null로 보내 저장된 값을 그대로 둔다", () => {
    const { store } = setup();
    expect(draftPayload(store.getState()).env_vars).toEqual([
      { name: "BASE", secret: false, value: "https://x" },
      { name: "TOKEN", secret: true, value: null },
    ]);
  });

  it("저장 응답의 updated_at과 이슈를 받는다", () => {
    const { store, actions } = setup();
    actions.updateNode("start", { title: "처음" });
    const revision = store.getState().revision;
    actions.markSaved(revision, { ...draft(), updated_at: "2026-10-14T00:00:01Z", issues: [] });
    expect(isDirty(store.getState())).toBe(false);
    expect(store.getState().updatedAt).toBe("2026-10-14T00:00:01Z");
    expect(store.getState().issueCounts.size).toBe(0);
  });
});
