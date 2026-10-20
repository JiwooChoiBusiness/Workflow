import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/http";
import type { WorkflowVersionOut } from "@/api/types";
import "@/features/workflows/i18n";
import i18n from "@/i18n";
import {
  dslErrorText,
  dslFilename,
  isImportedCode,
  localDslProblem,
  MAX_DSL_BYTES,
  summarizeImport,
  withoutImportedCode,
  type WorkflowImportOut,
} from "./dsl";
import { createFlowStore, isDirty } from "./flowStore";
import { draftPayload } from "./useDraftAutosave";

const t = i18n.getFixedT("ko", "workflows");
const tc = i18n.getFixedT("ko", "common");

// 서버가 Dify 예제(apps/api/tests/fixtures/dsl/dify/chatflow_support.yaml)를 가져오고 준 응답(2026-10-14)
const difyImport: WorkflowImportOut = {
  workflow_id: "01a13a45-0195-79c6-8736-8b7a6bcc062e",
  issues: [
    {
      level: "error",
      code: "unsupported_node",
      message: "가져오지 못한 knowledge-retrieval 노드다. 지원하는 노드로 바꿔 끼우거나 지운다",
      node_id: "n1730000000003",
      edge_id: null,
      field: "type",
    },
    {
      level: "error",
      code: "unsupported_node",
      message: "가져오지 못한 tool 노드다. 지원하는 노드로 바꿔 끼우거나 지운다",
      node_id: "n1730000000009",
      edge_id: null,
      field: "type",
    },
  ],
  converted: {
    source: "dify",
    nodes: 10,
    unsupported: ["n1730000000003", "n1730000000009"],
    warnings: [
      {
        code: "model_replaced",
        message: "모델 anthropic/claude-3-haiku 대신 Gemini 폴백 목록의 첫 모델을 쓴다",
        node_id: "n1730000000002",
      },
      {
        code: "unsupported_node",
        message: "Dify knowledge-retrieval 노드는 지원하지 않는 노드다. 바꿔 끼우거나 지운다",
        node_id: "n1730000000003",
      },
      {
        code: "model_replaced",
        message: "모델 openai/gpt-4o 대신 Gemini 폴백 목록의 첫 모델을 쓴다",
        node_id: "n1730000000004",
      },
      {
        code: "unsupported_node",
        message: "Dify tool 노드는 아직 옮기지 않는 노드다. 바꿔 끼우거나 지운다",
        node_id: "n1730000000009",
      },
      {
        code: "features_ignored",
        message: "가져오지 않은 Dify 앱 기능: suggested_questions_after_answer",
        node_id: null,
      },
    ],
  },
};

function fail(code: string, message: string, details?: unknown, status = 422): ApiError {
  return new ApiError(status, code, message, details);
}

describe("내보낸 파일 이름", () => {
  it("한글·이모지 이름은 filename*에서 꺼낸다", () => {
    const header =
      "attachment; filename=\"workflow-2.yml\"; filename*=UTF-8''" +
      "%EC%A3%BC%EC%A0%9C%EB%B3%84%20%EC%9A%94%EC%95%BD%20%F0%9F%A7%AA.yml";
    expect(dslFilename(header, "x.yml")).toBe("주제별 요약 🧪.yml");
  });

  it("filename*가 없거나 깨졌으면 ASCII 이름, 그것도 없으면 대신 쓸 이름", () => {
    expect(dslFilename('attachment; filename="flow.yml"', "x.yml")).toBe("flow.yml");
    expect(dslFilename("attachment; filename=flow.yml", "x.yml")).toBe("flow.yml");
    const broken = "attachment; filename=\"flow.yml\"; filename*=UTF-8''%E0%A4%A.yml";
    expect(dslFilename(broken, "x.yml")).toBe("flow.yml");
    expect(dslFilename("attachment", "slug.yml")).toBe("slug.yml");
    expect(dslFilename(null, "slug.yml")).toBe("slug.yml");
  });
});

describe("보내기 전 검사", () => {
  const file = (name: string, size: number) => new File([new Uint8Array(size)], name);

  it("비었거나 YAML 파일이 아니면 보내지 않는다", () => {
    expect(localDslProblem(null)).toBe("empty");
    expect(localDslProblem({ kind: "text", text: " \n " })).toBe("empty");
    expect(localDslProblem({ kind: "file", file: file("flow.json", 10) })).toBe("extension");
    expect(localDslProblem({ kind: "file", file: file("빈.yaml", 0) })).toBe("empty");
    expect(localDslProblem({ kind: "file", file: file("FLOW.YML", 10) })).toBeNull();
    expect(localDslProblem({ kind: "text", text: "dsl_version: 1" })).toBeNull();
  });

  it("2MB는 서버처럼 UTF-8 바이트로 잰다", () => {
    expect(localDslProblem({ kind: "file", file: file("a.yaml", MAX_DSL_BYTES) })).toBeNull();
    expect(localDslProblem({ kind: "file", file: file("a.yaml", MAX_DSL_BYTES + 1) })).toBe(
      "dsl_too_large",
    );
    // 글자 수는 70만이지만 한글은 3바이트라 2MB를 넘는다.
    const korean = "가".repeat(700_000);
    expect(korean.length).toBeLessThan(MAX_DSL_BYTES);
    expect(localDslProblem({ kind: "text", text: korean })).toBe("dsl_too_large");
  });
});

describe("가져오기 거부 사유", () => {
  it("2MB 초과·모르는 형식·종류 다름은 문장만 보인다", () => {
    const tooLarge = fail("dsl_too_large", "DSL 파일은 2MB까지다", { max_bytes: MAX_DSL_BYTES });
    expect(dslErrorText(tooLarge, t, tc)).toEqual({
      title: "파일이 2MB를 넘어 가져오지 못했습니다.",
      detail: null,
    });
    const unknown = fail("unknown_dsl_format", "우리 DSL도 Dify DSL도 아닌 YAML이다");
    expect(dslErrorText(unknown, t, tc).detail).toBeNull();
    const kind = fail("kind_mismatch", "chatflow DSL은 workflow에 덮어쓸 수 없다", null, 409);
    expect(dslErrorText(kind, t, tc)).toEqual({
      title: t("dsl.errors.kind_mismatch"),
      detail: null,
    });
  });

  it("깨진 YAML과 새 DSL 버전은 서버가 준 행·열과 버전을 함께 보인다", () => {
    const message = "YAML을 읽지 못했다(1행 9열): expected ',' or ']', but got '<stream end>'";
    expect(dslErrorText(fail("invalid_yaml", message, { line: 1, column: 9 }), t, tc)).toEqual({
      title: "YAML을 읽지 못했습니다. 들여쓰기·따옴표·괄호를 확인하세요.",
      detail: message,
    });
    const version = "이 서버는 dsl_version 1까지 읽는다(파일은 9). 서버를 올린 뒤 가져온다";
    expect(dslErrorText(fail("unsupported_dsl_version", version), t, tc)).toEqual({
      title: "이 서버보다 새 DSL 버전으로 만든 파일입니다. 서버를 올린 뒤 가져오세요.",
      detail: version,
    });
  });

  it("형식 오류는 틀린 칸을 세 개까지, 새 버전 노드는 노드마다 보인다", () => {
    const details = [
      { loc: ["name"], msg: "Field required", type: "missing" },
      { loc: ["graph", "nodes", 0, "id"], msg: "Input should be a valid string" },
      { loc: ["graph", "nodes", 0, "type"], msg: "Field required", type: "missing" },
      { loc: ["graph", "edges", 2], msg: "Input should be a valid dictionary" },
    ];
    const invalid = fail("invalid_dsl", "DSL 내용이 형식에 맞지 않는다", details);
    expect(dslErrorText(invalid, t, tc)).toEqual({
      title: "DSL 내용이 형식에 맞지 않습니다.",
      detail: [
        "name: Field required",
        "graph.nodes.0.id: Input should be a valid string",
        "graph.nodes.0.type: Field required",
        "그 밖에 1개",
      ].join("\n"),
    });
    const tooNew = [{ node_id: "s", type: "start", version: 99, latest: 1 }];
    expect(dslErrorText(fail("node_version_too_new", "새 버전", tooNew), t, tc)).toEqual({
      title: "이 서버보다 새 버전의 노드가 들어 있습니다. 서버를 올린 뒤 가져오세요.",
      detail: "s(start) 버전 99, 이 서버는 1까지",
    });
  });

  it("DSL 거부가 아닌 오류는 공통 오류 문장을 쓴다", () => {
    const taken = fail("slug_taken", "slug가 다른 워크플로우와 겹친다", undefined, 409);
    expect(dslErrorText(taken, t, tc)).toEqual({
      title: "slug가 다른 워크플로우와 겹친다",
      detail: null,
    });
    const offline = new ApiError(0, "network_error", "Failed to fetch");
    expect(dslErrorText(offline, t, tc).title).toBe(tc("errors.network"));
  });
});

describe("가져오기 결과 요약", () => {
  it("옮기지 못한 노드에는 변환 경고를 이유로 붙이고 경고 목록에서는 뺀다", () => {
    const summary = summarizeImport(difyImport);
    expect(summary.source).toBe("dify");
    expect(summary.nodes).toBe(10);
    expect(summary.unsupported).toEqual([
      {
        nodeId: "n1730000000003",
        reason: "Dify knowledge-retrieval 노드는 지원하지 않는 노드다. 바꿔 끼우거나 지운다",
      },
      {
        nodeId: "n1730000000009",
        reason: "Dify tool 노드는 아직 옮기지 않는 노드다. 바꿔 끼우거나 지운다",
      },
    ]);
    expect(summary.warnings.map((warning) => warning.code)).toEqual([
      "model_replaced",
      "model_replaced",
      "features_ignored",
    ]);
    expect(summary.errors.map((issue) => issue.node_id)).toEqual([
      "n1730000000003",
      "n1730000000009",
    ]);
    expect(summary.issueWarnings).toEqual([]);
  });

  it("이유가 없는 노드는 null이고 검증 오류와 경고를 나눠 모은다", () => {
    const summary = summarizeImport({
      workflow_id: "w1",
      issues: [
        { level: "warning", code: "type_mismatch", message: "타입", node_id: "a", field: "x" },
        { level: "error", code: "imported_code_unreviewed", message: "코드", node_id: "split" },
      ],
      converted: {
        source: "dsl",
        nodes: 3,
        unsupported: ["u1"],
        warnings: [{ code: "imported_code", message: "code 노드 1개", node_id: null }],
      },
    });
    expect(summary.unsupported).toEqual([{ nodeId: "u1", reason: null }]);
    expect(summary.warnings.map((warning) => warning.code)).toEqual(["imported_code"]);
    expect(summary.errors.map((issue) => issue.code)).toEqual(["imported_code_unreviewed"]);
    expect(summary.issueWarnings.map((issue) => issue.code)).toEqual(["type_mismatch"]);
  });
});

describe("가져온 code 노드", () => {
  it("code 노드의 ui.imported_code가 정확히 true일 때만 검토 대상이다", () => {
    expect(isImportedCode({ type: "code", ui: { imported_code: true } })).toBe(true);
    expect(isImportedCode({ type: "code", ui: { imported_code: "true" } })).toBe(false);
    expect(isImportedCode({ type: "code", ui: {} })).toBe(false);
    expect(isImportedCode({ type: "template", ui: { imported_code: true } })).toBe(false);
  });

  it("검토 완료는 그 키만 지운 초안을 자동 저장에 올린다", () => {
    const draft: WorkflowVersionOut = {
      id: "v0",
      workflow_id: "w1",
      kind: "workflow",
      version_number: null,
      status: "draft",
      graph: {
        nodes: [
          {
            id: "split",
            type: "code",
            version: 1,
            title: "나누기",
            config: { code: "def main():\n    return {}\n", inputs: [], outputs: [] },
            position: { x: 0, y: 0 },
            ui: { imported_code: true, note: "메모" },
          },
        ],
        edges: [],
        viewport: {},
      },
      env_vars: [],
      conversation_vars: [],
      settings: { approval_mode: "inherit", mask_secrets_in_outputs: true },
      graph_hash: "h",
      published_at: null,
      change_note: null,
      issues: [],
      created_at: "2026-10-14T00:00:00Z",
      updated_at: "2026-10-14T00:00:00Z",
    };
    const store = createFlowStore("w1", "workflow");
    store.getState().actions.load(draft);
    const node = store.getState().graph.nodes[0]!;
    expect(isImportedCode(node)).toBe(true);
    expect(isDirty(store.getState())).toBe(false);

    store.getState().actions.updateNode("split", { ui: withoutImportedCode(node.ui) });
    expect(isDirty(store.getState())).toBe(true);
    expect(draftPayload(store.getState()).graph.nodes?.[0]?.ui).toEqual({ note: "메모" });
    expect(node.ui).toEqual({ imported_code: true, note: "메모" });
  });
});
