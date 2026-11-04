/*
 * 노드 폼의 config가 서버 Config 모델(GET /api/console/node-types의 config_schema)을 지키는지 본다.
 * 픽스처는 LLM_FAKE=true 서버에서 받은 응답이다(2026-10-15, M19의 2차 노드와 내장 도구 tools까지). 서버
 * 노드 config가 바뀌면 픽스처를 다시 받고 이 테스트가 어긋난 곳을 알려 준다.
 */
import { describe, expect, it } from "vitest";
import { VAR_TYPES } from "@/api/types";
import "@/features/workflows/i18n";
import i18n from "@/i18n";
import nodeTypeList from "@/test/fixtures/contract/node-types.json";
import llmSource from "../../../../api/app/workflow/nodes/llm.py?raw";
import {
  agentFromForm,
  agentSchema,
  agentToForm,
  aggregatorFromForm,
  aggregatorToForm,
  answerFromForm,
  answerToForm,
  ASSIGN_OPS,
  assignerFromForm,
  assignerSchema,
  assignerToForm,
  classifierFromForm,
  classifierSchema,
  classifierToForm,
  codeFromForm,
  codeToForm,
  CONTEXT_ACCEPT,
  CONTEXT_TYPES,
  DEFAULT_KNOWLEDGE_LIMITS,
  documentExtractorFromForm,
  documentExtractorToForm,
  DUPLICATE_POLICIES,
  endFromForm,
  endSchema,
  endToForm,
  extractorFromForm,
  extractorToForm,
  HTTP_AUTH_TYPES,
  HTTP_BODY_TYPES,
  HTTP_METHODS,
  httpFromForm,
  httpSchema,
  httpToForm,
  humanInputFromForm,
  humanInputSchema,
  humanInputToForm,
  IF_ELSE_OPS,
  ifElseFromForm,
  ifElseSchema,
  ifElseToForm,
  ITERATION_ERROR_MODES,
  iterationFromForm,
  iterationToForm,
  knowledgeIndexFromForm,
  knowledgeIndexSchema,
  knowledgeIndexToForm,
  knowledgeRetrievalFromForm,
  knowledgeRetrievalSchema,
  knowledgeRetrievalToForm,
  LIST_SORT_ORDERS,
  listOperatorFromForm,
  listOperatorSchema,
  listOperatorToForm,
  llmFromForm,
  llmSchema,
  llmToForm,
  loopFromForm,
  loopSchema,
  loopToForm,
  opsFor,
  PARAM_TYPES,
  PROMPT_ROLES,
  SQL_APPROVALS,
  SQL_QUERY_MAX_ROWS,
  sqlQueryFromForm,
  sqlQuerySchema,
  sqlQueryToForm,
  sqlSchemaFromForm,
  sqlSchemaSchema,
  sqlSchemaToForm,
  START_FIELD_TYPES,
  startFromForm,
  startSchema,
  startToForm,
  templateFromForm,
  templateToForm,
  TOOL_ARG_MODES,
  toolFromForm,
  toolSchema,
  toolToForm,
  VAR_TYPE_OPTIONS,
} from "./configSchemas";
import {
  FILTER_MODES,
  MAX_FILTER_CONDITIONS,
  type FilterField,
} from "@/features/knowledge/metadataFilter";
import { DEFAULT_RULE, toProcessRule } from "@/features/knowledge/processRule";
import {
  RERANK_CANDIDATES_MAX,
  RERANKING_MODES,
  SEARCH_METHODS,
} from "@/features/knowledge/retrievalSettings";
import { assignable } from "./graph";
import {
  DEFAULT_SYSTEM_PROMPT,
  defaultConfig,
  INDEX_WAIT_DEFAULT_SEC,
  PALETTE_ORDER,
} from "./nodeTypes";

const t = i18n.getFixedT("ko", "common");
const tw = i18n.getFixedT("ko", "workflows");

type Schema = Record<string, unknown>;

// 픽스처가 쓰는 만큼의 JSON Schema 검사기(type, properties, required, additionalProperties, items, enum,
// anyOf, $ref, 길이·개수·범위, pattern).
function resolve(schema: Schema, root: Schema): Schema {
  const ref = schema.$ref;
  if (typeof ref !== "string") return schema;
  const name = ref.split("/").pop() ?? "";
  return resolve((root.$defs as Record<string, Schema>)[name] ?? {}, root);
}

function check(value: unknown, input: Schema, root: Schema, path: string, errors: string[]): void {
  const schema = resolve(input, root);
  if (Array.isArray(schema.anyOf)) {
    const ok = (schema.anyOf as Schema[]).some((option) => {
      const found: string[] = [];
      check(value, option, root, path, found);
      return found.length === 0;
    });
    if (!ok) errors.push(`${path}: anyOf에 맞지 않음 ${JSON.stringify(value)}`);
    return;
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push(`${path}: enum 밖 ${JSON.stringify(value)}`);
  }
  switch (schema.type) {
    case "object": {
      if (typeof value !== "object" || value === null || Array.isArray(value)) {
        errors.push(`${path}: object가 아님`);
        return;
      }
      const record = value as Record<string, unknown>;
      const properties = (schema.properties ?? {}) as Record<string, Schema>;
      for (const key of (schema.required ?? []) as string[]) {
        if (!(key in record)) errors.push(`${path}.${key}: 필수`);
      }
      for (const [key, item] of Object.entries(record)) {
        if (properties[key]) check(item, properties[key], root, `${path}.${key}`, errors);
        else if (schema.additionalProperties === false) errors.push(`${path}.${key}: 모르는 키`);
        else if (typeof schema.additionalProperties === "object") {
          check(item, schema.additionalProperties as Schema, root, `${path}.${key}`, errors);
        }
      }
      return;
    }
    case "array": {
      if (!Array.isArray(value)) {
        errors.push(`${path}: array가 아님`);
        return;
      }
      if (typeof schema.minItems === "number" && value.length < schema.minItems) {
        errors.push(`${path}: ${schema.minItems}개 미만`);
      }
      if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
        errors.push(`${path}: ${schema.maxItems}개 초과`);
      }
      if (schema.items) {
        value.forEach((item, index) =>
          check(item, schema.items as Schema, root, `${path}[${index}]`, errors),
        );
      }
      return;
    }
    case "string":
      if (typeof value !== "string") {
        errors.push(`${path}: string이 아님 ${JSON.stringify(value)}`);
        return;
      }
      if (typeof schema.minLength === "number" && value.length < schema.minLength) {
        errors.push(`${path}: 너무 짧음`);
      }
      if (typeof schema.maxLength === "number" && value.length > schema.maxLength) {
        errors.push(`${path}: 너무 김`);
      }
      if (typeof schema.pattern === "string" && !new RegExp(schema.pattern, "u").test(value)) {
        errors.push(`${path}: pattern ${schema.pattern}`);
      }
      return;
    case "integer":
    case "number":
      if (
        typeof value !== "number" ||
        !Number.isFinite(value) ||
        (schema.type === "integer" && !Number.isInteger(value))
      ) {
        errors.push(`${path}: ${schema.type}가 아님 ${JSON.stringify(value)}`);
        return;
      }
      if (typeof schema.minimum === "number" && value < schema.minimum)
        errors.push(`${path}: 최솟값`);
      if (typeof schema.maximum === "number" && value > schema.maximum)
        errors.push(`${path}: 최댓값`);
      if (typeof schema.exclusiveMinimum === "number" && value <= schema.exclusiveMinimum) {
        errors.push(`${path}: 초과해야 함`);
      }
      return;
    case "boolean":
      if (typeof value !== "boolean") errors.push(`${path}: boolean이 아님`);
      return;
    case "null":
      if (value !== null) errors.push(`${path}: null이 아님`);
      return;
    default:
      return;
  }
}

const schemas = new Map(
  nodeTypeList.items.map((item) => [item.type, item.config_schema as unknown as Schema]),
);

function problems(type: string, config: unknown): string[] {
  const schema = schemas.get(type);
  if (!schema) throw new Error(`픽스처에 ${type}가 없다`);
  const errors: string[] = [];
  check(config, schema, schema, type, errors);
  return errors;
}

// 경로의 "[]"는 배열 원소, "*"는 additionalProperties(이름이 정해지지 않은 키), anyOf는 null이 아닌 쪽이다.
function enumAt(type: string, ...path: string[]): unknown[] {
  const root = schemas.get(type)!;
  let current: Schema = root;
  for (const key of path) {
    current = resolve(current, root);
    if (Array.isArray(current.anyOf)) {
      current = resolve(
        (current.anyOf as Schema[]).find((option) => option.type !== "null")!,
        root,
      );
    }
    if (key === "[]") current = current.items as Schema;
    else if (key === "*") current = current.additionalProperties as Schema;
    else current = (current.properties as Record<string, Schema>)[key]!;
  }
  current = resolve(current, root);
  return (current.enum ?? []) as unknown[];
}

const tools = nodeTypeList.tools;

const labels = { className: (n: number) => `분류 ${n}` };
const AGENT_ID = "01a1391c-6458-7b30-9a21-310217cd8e29";
const KB_ID = "019a4b2c-1111-7000-8000-000000000001";
const KB_ID_2 = "019a4b2c-2222-7000-8000-000000000002";
const SOURCE_ID = "019a4b2c-3333-7000-8000-000000000003";
const kbOptions = nodeTypeList.kb_options;

// 화면에서 사용자가 다 채웠을 때 나올 법한 config. 폼 값으로 바꿨다가 되돌린 결과를 서버 스키마로 본다.
const SAMPLES: Record<
  string,
  [Record<string, unknown>, (config: Record<string, unknown>) => unknown]
> = {
  start: [
    {
      fields: [
        {
          name: "q",
          type: "paragraph",
          label: "질문",
          required: true,
          default: null,
          max_length: 500,
        },
        { name: "n", type: "number", label: "", required: false, default: 3 },
        {
          name: "kind",
          type: "select",
          label: "",
          required: false,
          default: "a",
          options: ["a", "b"],
        },
        { name: "ok", type: "boolean", label: "", required: false, default: true },
        { name: "doc", type: "file", label: "", required: false },
        { name: "docs", type: "file_list", label: "", required: false },
      ],
    },
    (config) => startFromForm(startToForm(config)),
  ],
  end: [{ outputs: [{ name: "answer", var: "llm_1.text" }] }, (c) => endFromForm(endToForm(c))],
  answer: [{ text: "답: {{ llm_1.text }}" }, (c) => answerFromForm(answerToForm(c))],
  template: [
    { template: "{{ q }}!", vars: [{ name: "q", var: "start.q" }] },
    (c) => templateFromForm(templateToForm(c)),
  ],
  if_else: [
    {
      cases: [
        {
          id: "1",
          logical: "or",
          conditions: [
            { var: "start.q", op: "contains", value: "환불" },
            { var: "start.n", op: "≥", value: "3" },
            { var: "start.kind", op: "in", value: ["a", "b"] },
            { var: "start.q", op: "is_empty", value: null },
          ],
        },
        { id: "2", logical: "and", conditions: [{ var: "start.q", op: "regex", value: "^x" }] },
      ],
    },
    (c) => ifElseFromForm(ifElseToForm(c)),
  ],
  variable_aggregator: [
    { vars: ["llm_1.text", "llm_2.text"], output_type: "string" },
    (c) => aggregatorFromForm(aggregatorToForm(c)),
  ],
  variable_assigner: [
    {
      items: [
        { target: "conv.topic", op: "overwrite", var: "start.q" },
        { target: "conv.count", op: "+=", value: 1 },
        { target: "conv.tags", op: "append", value: "새 태그" },
        { target: "conv.memo", op: "clear" },
      ],
    },
    (c) =>
      assignerFromForm(assignerToForm(c), {
        topic: "string",
        count: "number",
        tags: "array[string]",
        memo: "string",
      }),
  ],
  llm: [
    {
      model: {
        model_id: "gemini-3.8-flash",
        params: { temperature: 0.2, top_p: null, max_output_tokens: 2048, reasoning_effort: "low" },
      },
      prompt: [
        { role: "system", text: DEFAULT_SYSTEM_PROMPT },
        { role: "user", text: "{{ start.q }}" },
      ],
      memory: { enabled: true, window_turns: 5 },
      vision: { enabled: true, var: "start.doc" },
      json_mode: true,
      output_schema: { type: "object", properties: { a: { type: "string" } } },
      context: { var: "knowledge_retrieval_1.result" },
    },
    (c) => llmFromForm(llmToForm(c)),
  ],
  knowledge_retrieval: [
    {
      kb_ids: [KB_ID, KB_ID_2],
      query_var: "start.q",
      retrieval: {
        search_method: "hybrid_search",
        top_k: 6,
        score_threshold_enabled: true,
        score_threshold: 0.4,
        reranking_mode: "llm",
        weights: { vector_weight: 0.6, keyword_weight: 0.4 },
        rerank: { model_id: "gemini-3.8-flash", candidates: 12 },
      },
      metadata_filter: {
        mode: "manual",
        logical_operator: "or",
        conditions: [
          { name: "category", comparison_operator: "is", value: "규정" },
          { name: "year", comparison_operator: "≥", value: 2024 },
          { name: "tags", comparison_operator: "in", value: ["a", "b"] },
          { name: "owner", comparison_operator: "is", value: { var: "start.q" } },
          { name: "note", comparison_operator: "empty", value: null },
        ],
        model: { model_id: null },
      },
    },
    (c) => knowledgeRetrievalFromForm(knowledgeRetrievalToForm(c)),
  ],
  knowledge_index: [
    {
      kb_id: KB_ID,
      source: { text_var: null, files_var: "start.docs" },
      name: "{{ sys.now }} 답",
      process_rule: toProcessRule(DEFAULT_RULE),
      metadata: [{ name: "source_url", value: "{{ start.q }}" }],
      on_duplicate: "skip",
      wait: true,
      timeout_sec: 120,
    },
    (c) => knowledgeIndexFromForm(knowledgeIndexToForm(c)),
  ],
  sql_schema: [
    {
      source_id: SOURCE_ID,
      tables: ["customers", "orders"],
      include_samples: false,
      max_tokens: 12000,
    },
    (c) => sqlSchemaFromForm(sqlSchemaToForm(c)),
  ],
  sql_query: [
    {
      source_id: SOURCE_ID,
      sql: "{{ write_sql.text }}",
      sql_var: null,
      max_rows: 500,
      timeout_sec: 20,
      approval: "always",
      llm_rows: 30,
      llm_bytes: 32768,
      export_file: false,
    },
    (c) => sqlQueryFromForm(sqlQueryToForm(c)),
  ],
  http_request: [
    {
      method: "POST",
      url: "https://api.example.com/{{ start.q }}",
      headers: [{ name: "X-Trace", value: "1" }],
      params: [{ name: "page", value: "{{ start.n }}" }],
      body: { type: "json", content: '{"q": {{ start.q | tojson }}}' },
      auth: { type: "header", value: "{{ env.TOKEN }}", header: "X-Api-Key" },
      timeout_sec: 30,
      max_body_bytes: 1048576,
    },
    (c) => httpFromForm(httpToForm(c)),
  ],
  code: [
    {
      code: "def main(a):\n    return {'r': a}\n",
      inputs: [{ name: "a", var: "start.q" }],
      outputs: [{ name: "r", type: "string" }],
      timeout_sec: 10,
    },
    (c) => codeFromForm(codeToForm(c)),
  ],
  question_classifier: [
    {
      model: {
        model_id: null,
        params: { temperature: null, top_p: null, max_output_tokens: 1024, reasoning_effort: null },
      },
      query_var: "start.q",
      classes: [
        { id: "1", name: "환불", description: "환불 요청" },
        { id: "2", name: "기타", description: "" },
      ],
      instruction: "",
      on_parse_failure: "fail",
    },
    (c) => classifierFromForm(classifierToForm(c)),
  ],
  parameter_extractor: [
    {
      model: {
        model_id: null,
        params: { temperature: null, top_p: null, max_output_tokens: 1024, reasoning_effort: null },
      },
      query_var: "start.q",
      parameters: [
        {
          name: "city",
          type: "string",
          description: "도시",
          required: true,
          enum: ["서울", "부산"],
        },
        { name: "days", type: "number", description: "", required: false, enum: [1, 2, 3] },
        { name: "tags", type: "array[string]", description: "", required: false, enum: null },
      ],
      instruction: "",
      reasoning_mode: "prompt",
    },
    (c) => extractorFromForm(extractorToForm(c)),
  ],
  iteration: [
    {
      input_var: "start.docs",
      output_var: "llm_9.text",
      parallel: true,
      max_parallel: 3,
      error_mode: "remove",
    },
    (c) => iterationFromForm(iterationToForm(c)),
  ],
  agent: [
    {
      agent_id: AGENT_ID,
      agent_version_id: null,
      instruction: "{{ start.q }}을 정리한다",
      inputs: [{ name: "q", var: "start.q" }],
      files_var: "start.docs",
      result: { format: "json", schema: { type: "object", properties: {} }, files: false },
      limits: { max_steps: 10, max_cost_usd: 0.5 },
      approval_mode: "off",
      session_mode: "ephemeral",
    },
    (c) => agentFromForm(agentToForm(c)),
  ],
  loop: [
    {
      loop_vars: [
        { name: "text", type: "string", value: "" },
        { name: "n", type: "number", value: 0 },
        { name: "topic", type: "string", var: "start.q" },
        { name: "items", type: "array[string]", value: ["a"] },
        { name: "last", type: "any", value: null },
      ],
      break_conditions: [
        { var: "loop_1.n", op: "≥", value: "3" },
        { var: "loop_start_1.index", op: "=", value: "2" },
      ],
      logical: "or",
      max_iterations: 100,
    },
    (c) => loopFromForm(loopToForm(c)),
  ],
  list_operator: [
    {
      input_var: "start.docs",
      filter: {
        logical: "and",
        conditions: [
          { key: "extension", op: "=", value: "pdf" },
          { key: "size", op: ">", value: "10" },
          { key: "name", op: "in", value: ["a.pdf", "b.pdf"] },
        ],
      },
      sort: { key: "size", order: "desc" },
      slice: { offset: 1, limit: 2 },
      extract_index: -1,
    },
    (c) => listOperatorFromForm(listOperatorToForm(c)),
  ],
  document_extractor: [
    { file_var: "start.docs" },
    (c) => documentExtractorFromForm(documentExtractorToForm(c)),
  ],
  tool: [
    {
      tool: "regex_extract",
      params: {
        text: { mode: "var", value: "start.q" },
        pattern: { mode: "const", value: "\\d+" },
        flags: { mode: "template", value: "{{ start.flags }}" },
        group: { mode: "const", value: "" },
      },
    },
    (c) => toolFromForm(toolToForm(c), tools),
  ],
  human_input: [
    {
      description: "앞부분: {{ llm_1.text }}",
      fields: [
        { name: "ok", type: "boolean", label: "괜찮은가", required: true, default: null },
        { name: "note", type: "paragraph", label: "메모", required: false, max_length: 500 },
        { name: "pick", type: "select", label: "", required: false, options: ["좋음", "나쁨"] },
        { name: "proof", type: "file", label: "", required: false },
      ],
    },
    (c) => humanInputFromForm(humanInputToForm(c)),
  ],
};

describe("서버 config_schema 계약", () => {
  it("픽스처의 모든 타입에 폼이나 자리가 있다", () => {
    const types = nodeTypeList.items
      .map((item) => item.type)
      .filter((type) => type !== "iteration_start" && type !== "loop_start");
    expect([...types].sort()).toEqual([...PALETTE_ORDER].sort());
    expect(Object.keys(SAMPLES).sort()).toEqual([...PALETTE_ORDER].sort());
  });

  it.each(Object.keys(SAMPLES))("%s: 채운 폼의 config가 서버 스키마를 지킨다", (type) => {
    const [config, roundTrip] = SAMPLES[type]!;
    expect(problems(type, roundTrip(config))).toEqual([]);
  });

  it.each(Object.keys(SAMPLES))("%s: 새 노드의 기본 config에 서버가 모르는 키가 없다", (type) => {
    for (const kind of ["workflow", "chatflow"] as const) {
      const extra = problems(type, defaultConfig(type, kind, labels)).filter((item) =>
        item.endsWith("모르는 키"),
      );
      expect(extra).toEqual([]);
    }
  });

  it("고를 수 있는 값이 서버 열거값과 같다(화면 순서는 따로 둔다)", () => {
    const same = (mine: readonly unknown[], server: unknown[]) =>
      expect([...mine].map(String).sort()).toEqual(server.map(String).sort());
    same(START_FIELD_TYPES, enumAt("start", "fields", "[]", "type"));
    same(IF_ELSE_OPS, enumAt("if_else", "cases", "[]", "conditions", "[]", "op"));
    same(ASSIGN_OPS, enumAt("variable_assigner", "items", "[]", "op"));
    same(PROMPT_ROLES, enumAt("llm", "prompt", "[]", "role"));
    same(HTTP_METHODS, enumAt("http_request", "method"));
    same(HTTP_BODY_TYPES, enumAt("http_request", "body", "type"));
    same(HTTP_AUTH_TYPES, enumAt("http_request", "auth", "type"));
    same(VAR_TYPE_OPTIONS, enumAt("code", "outputs", "[]", "type"));
    same(VAR_TYPE_OPTIONS, enumAt("variable_aggregator", "output_type"));
    same(PARAM_TYPES, enumAt("parameter_extractor", "parameters", "[]", "type"));
    same(ITERATION_ERROR_MODES, enumAt("iteration", "error_mode"));
    same(VAR_TYPE_OPTIONS, enumAt("loop", "loop_vars", "[]", "type"));
    same(IF_ELSE_OPS, enumAt("loop", "break_conditions", "[]", "op"));
    same(IF_ELSE_OPS, enumAt("list_operator", "filter", "conditions", "[]", "op"));
    same(LIST_SORT_ORDERS, enumAt("list_operator", "sort", "order"));
    same(TOOL_ARG_MODES, enumAt("tool", "params", "*", "mode"));
    same(START_FIELD_TYPES, enumAt("human_input", "fields", "[]", "type"));
    same(DUPLICATE_POLICIES, enumAt("knowledge_index", "on_duplicate"));
    same(FILTER_MODES, enumAt("knowledge_retrieval", "metadata_filter", "mode"));
    same(SEARCH_METHODS, enumAt("knowledge_retrieval", "retrieval", "search_method"));
    same(RERANKING_MODES, enumAt("knowledge_retrieval", "retrieval", "reranking_mode"));
  });

  it("지식 노드 폼의 선택지와 상한이 kb_options와 같다", () => {
    const same = (mine: readonly unknown[], server: readonly unknown[]) =>
      expect([...mine].map(String).sort()).toEqual([...server].map(String).sort());
    same(DUPLICATE_POLICIES, kbOptions.duplicate_policies);
    same(FILTER_MODES, kbOptions.filter_modes);
    same(SEARCH_METHODS, kbOptions.search_methods);
    same(RERANKING_MODES, kbOptions.reranking_modes);
    expect(DEFAULT_KNOWLEDGE_LIMITS).toEqual({
      maxKbs: kbOptions.max_kbs,
      topKMax: kbOptions.top_k_max,
      waitMaxSec: kbOptions.index_wait_max_sec,
    });
    expect(INDEX_WAIT_DEFAULT_SEC).toBe(kbOptions.index_wait_default_sec);
    expect(RERANK_CANDIDATES_MAX).toBe(kbOptions.rerank_candidates_max);
    expect(MAX_FILTER_CONDITIONS).toBe(kbOptions.max_conditions);
    // 필드 타입마다 고르는 연산자는 모두 조건 config가 받는 이름이다.
    const accepted = enumAt(
      "knowledge_retrieval",
      "metadata_filter",
      "conditions",
      "[]",
      "comparison_operator",
    );
    for (const list of Object.values(kbOptions.operators)) {
      for (const operator of list) expect(accepted).toContain(operator);
    }
  });

  it("고정값으로 채운 도구 매개변수가 그 도구의 params_schema를 지킨다", () => {
    const samples: Record<string, string> = {
      timezone: "Asia/Seoul",
      format: "%Y-%m-%d",
      text: '{"a": 1}',
      path: "a",
      pattern: "\\d+",
      flags: "i",
      group: "1",
      url: "https://example.com",
      max_length: "5000",
    };
    for (const tool of tools) {
      const form = {
        tool: tool.name,
        params: Object.fromEntries(
          tool.params.map((param) => [
            param.name,
            { mode: "const" as const, value_text: samples[param.name] ?? "x" },
          ]),
        ),
      };
      const config = toolFromForm(form, tools);
      const values = Object.fromEntries(
        Object.entries(config.params).map(([name, arg]) => [name, arg.value]),
      );
      const errors: string[] = [];
      const schema = tool.params_schema as Schema;
      check(values, schema, schema, tool.name, errors);
      expect(errors).toEqual([]);
      expect(problems("tool", config)).toEqual([]);
    }
  });

  it("새 llm 노드의 시스템 프롬프트가 서버 DEFAULT_SYSTEM_PROMPT와 같다", () => {
    const block = /DEFAULT_SYSTEM_PROMPT = \(([\s\S]*?)\)/.exec(llmSource)?.[1] ?? "";
    const server = [...block.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1]).join("");
    expect(server).toBe(DEFAULT_SYSTEM_PROMPT);
    const prompt = defaultConfig("llm", "workflow", labels).prompt as {
      role: string;
      text: string;
    }[];
    expect(prompt[0]).toEqual({ role: "system", text: DEFAULT_SYSTEM_PROMPT });
  });
});

describe("폼 검증", () => {
  it("이름 겹침·선택자 형식을 칸에 붙인다", () => {
    const result = endSchema(t, tw).safeParse({
      outputs: [
        { name: "a", var: "llm_1.text" },
        { name: "a", var: "" },
        { name: "1bad", var: "not a selector" },
      ],
    });
    expect(result.success).toBe(false);
    const paths = result.error!.issues.map((issue) => issue.path.join("."));
    expect(paths).toEqual(
      expect.arrayContaining([
        "outputs.1.name",
        "outputs.1.var",
        "outputs.2.name",
        "outputs.2.var",
      ]),
    );
  });

  it("조건 빌더는 비교 값이 필요한 연산자와 숫자 비교를 본다", () => {
    const base = { id: "1", logical: "and" as const };
    const result = ifElseSchema(t, tw).safeParse({
      cases: [
        {
          ...base,
          conditions: [
            { var: "start.q", op: ">", value_text: "abc" },
            { var: "start.q", op: "contains", value_text: "" },
            { var: "start.q", op: "is_empty", value_text: "" },
          ],
        },
      ],
    });
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "cases.0.conditions.0.value_text",
      "cases.0.conditions.1.value_text",
    ]);
    expect(opsFor("boolean")).toEqual(["=", "≠", "is_empty", "is_not_empty"]);
    expect(opsFor("array[string]")).toEqual([
      "contains",
      "not_contains",
      "is_empty",
      "is_not_empty",
    ]);
    expect(opsFor(null)).toEqual(IF_ELSE_OPS);
  });

  it("llm은 사용자 블록과 출력 스키마를 본다", () => {
    const values = llmToForm({
      prompt: [{ role: "system", text: "x" }],
      output_schema: { type: "array" },
    });
    const result = llmSchema(t, tw).safeParse(values);
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual(
      expect.arrayContaining(["prompt", "output_schema_text"]),
    );
  });

  it("http는 URL과 헤더 이름, 인증 헤더를 본다", () => {
    const values = httpToForm({
      url: " ",
      headers: [{ name: "bad header", value: "" }],
      auth: { type: "header", value: "", header: "" },
    });
    const result = httpSchema(t, tw).safeParse(values);
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual(
      expect.arrayContaining(["url", "headers.0.name", "auth.header"]),
    );
  });

  it("시작 필드는 선택지 없는 select와 숫자가 아닌 기본값을 막는다", () => {
    const values = startToForm({
      fields: [
        { name: "s", type: "select", options: [] },
        { name: "n", type: "number", default: "x" },
      ],
    });
    const result = startSchema(t, tw).safeParse(values);
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "fields.0.options_text",
      "fields.1.default_text",
    ]);
  });

  it("분류기는 클래스가 하나 이상이고 이름이 있어야 한다", () => {
    const values = classifierToForm({ query_var: "sys.query", classes: [{ id: "1", name: " " }] });
    const result = classifierSchema(t, tw).safeParse(values);
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual(["classes.0.name"]);
  });

  it("에이전트 노드는 에이전트를 골라야 하고 한도는 채운 칸만 보낸다", () => {
    const values = agentToForm({ instruction: "x" });
    expect(agentSchema(t, tw).safeParse(values).error!.issues[0]?.path).toEqual(["agent_id"]);
    const config = agentFromForm({
      ...values,
      agent_id: AGENT_ID,
      limits_enabled: true,
      limits: { ...values.limits, max_steps: 5 },
    });
    expect(config.limits).toEqual({ max_steps: 5 });
    expect(agentFromForm({ ...values, limits_enabled: false }).limits).toBeNull();
  });

  it("http 본문 형식을 바꾸면 그 형식의 내용만 보낸다", () => {
    const values = httpToForm({ body: { type: "form", content: [{ name: "a", value: "1" }] } });
    expect(httpFromForm({ ...values, body_type: "json", body_text: "{}" }).body).toEqual({
      type: "json",
      content: "{}",
    });
    expect(httpFromForm(values).body).toEqual({
      type: "form",
      content: [{ name: "a", value: "1" }],
    });
    expect(httpFromForm({ ...values, body_type: "none" }).body).toEqual({
      type: "none",
      content: "",
    });
  });

  it("대입 노드는 변수를 쓸 때 value 키를 넣지 않는다", () => {
    const config = assignerFromForm(
      assignerToForm({ items: [{ target: "conv.a", op: "overwrite", var: "start.q" }] }),
      { a: "string" },
    );
    expect(config.items[0]).toEqual({ target: "conv.a", op: "overwrite", var: "start.q" });
    expect("value" in config.items[0]!).toBe(false);
  });

  it("loop 안의 대입 노드는 반복 변수를 대상으로 받고 고정값을 그 타입으로 읽는다", () => {
    const values = assignerToForm({
      items: [
        { target: "loop_1.n", op: "+=", value: 1 },
        { target: "loop_1.text", op: "overwrite", value: "가" },
        { target: "conv.topic", op: "overwrite", value: "x" },
      ],
    });
    const inWorkflowLoop = assignerSchema(tw, { loopId: "loop_1", conv: false }).safeParse(values);
    expect(inWorkflowLoop.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "items.2.target",
    ]);
    expect(assignerSchema(tw, { loopId: "loop_1", conv: true }).safeParse(values).success).toBe(
      true,
    );
    expect(assignerSchema(tw).safeParse(values).success).toBe(false);
    const config = assignerFromForm(
      { items: [{ ...values.items[1]!, value_text: "12" }] },
      {},
      { id: "loop_1", types: { text: "string" } },
    );
    expect(config.items[0]).toEqual({ target: "loop_1.text", op: "overwrite", value: "12" });
  });

  it("loop는 예약 이름·빈 변수·숫자가 아닌 처음 값을 막고 빈 칸은 null로 보낸다", () => {
    const values = loopToForm({
      loop_vars: [
        { name: "loop_count", type: "number", value: 1 },
        { name: "a", type: "string", var: "" },
        { name: "b", type: "number", value: "x" },
      ],
    });
    expect(
      loopSchema(t, tw)
        .safeParse(values)
        .error!.issues.map((i) => i.path.join(".")),
    ).toEqual(["loop_vars.0.name", "loop_vars.1.var", "loop_vars.2.value_text"]);
    const config = loopFromForm({
      ...values,
      loop_vars: [{ name: "n", type: "number", source: "value", var: "", value_text: "" }],
    });
    expect(config.loop_vars).toEqual([{ name: "n", type: "number", value: null }]);
  });

  it("list_operator는 켠 단계만 보내고 켠 거르기에는 조건이 있어야 한다", () => {
    const values = listOperatorToForm({ input_var: "start.items" });
    expect(listOperatorFromForm(values)).toEqual({
      input_var: "start.items",
      filter: null,
      sort: null,
      slice: null,
      extract_index: null,
    });
    const result = listOperatorSchema(t, tw).safeParse({ ...values, filter_enabled: true });
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "filter.conditions",
    ]);
    const sliced = listOperatorFromForm({
      ...values,
      slice_enabled: true,
      slice: { offset: 2, limit: null },
      extract_enabled: true,
      extract_index: -1,
    });
    expect(sliced.slice).toEqual({ offset: 2, limit: null });
    expect(sliced.extract_index).toBe(-1);
  });

  it("tool은 기본값 없는 필수 매개변수와 변수 형식을 보고 다른 도구의 매개변수는 버린다", () => {
    const form = toolToForm({
      tool: "json_parse",
      params: {
        text: { mode: "var", value: "not a selector" },
        pattern: { mode: "const", value: "x" },
      },
    });
    const result = toolSchema(t, tw, tools).safeParse(form);
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "params.text.value_text",
    ]);
    const empty = toolSchema(t, tw, tools).safeParse({
      tool: "json_parse",
      params: { text: { mode: "template", value_text: " " } },
    });
    expect(empty.error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "params.text.value_text",
    ]);
    expect(Object.keys(toolFromForm(form, tools).params).sort()).toEqual(["path", "text"]);
    expect(toolSchema(t, tw, tools).safeParse({ tool: "", params: {} }).success).toBe(false);
  });

  it("지식 검색은 지식 베이스·질의 변수가 있어야 하고 여럿이면 검색 설정을 정해야 한다", () => {
    const schema = knowledgeRetrievalSchema(t, tw, DEFAULT_KNOWLEDGE_LIMITS);
    const empty = knowledgeRetrievalToForm(
      defaultConfig("knowledge_retrieval", "workflow", labels),
    );
    expect(schema.safeParse(empty).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "kb_ids",
      "query_var",
    ]);
    const two = { ...empty, kb_ids: [KB_ID, KB_ID_2], query_var: "start.q" };
    expect(schema.safeParse(two).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "custom_retrieval",
    ]);
    expect(schema.safeParse({ ...two, custom_retrieval: true }).success).toBe(true);
    // 챗플로우의 새 노드는 sys.query를 질의로 쓰고 검색 설정은 KB 것을 이어받는다.
    const chat = defaultConfig("knowledge_retrieval", "chatflow", labels);
    expect(chat).toMatchObject({ query_var: "sys.query", retrieval: null });
    expect(knowledgeRetrievalFromForm(knowledgeRetrievalToForm(chat))).toEqual(chat);
  });

  it("지식 검색의 수동 조건은 빈 값·고르지 않은 변수·숫자 아닌 값을 막고, 값은 필드 타입으로 보낸다", () => {
    const fields: FilterField[] = [
      { name: "category", type: "string" },
      { name: "year", type: "number" },
    ];
    const operators = nodeTypeList.kb_options.operators as Record<string, string[]>;
    const schema = knowledgeRetrievalSchema(t, tw, DEFAULT_KNOWLEDGE_LIMITS, { operators, fields });
    const values = knowledgeRetrievalToForm({
      kb_ids: [KB_ID],
      query_var: "start.q",
      metadata_filter: {
        mode: "manual",
        conditions: [
          { name: "category", comparison_operator: "is", value: { var: "" } },
          { name: "category", comparison_operator: "is", value: "" },
          { name: "year", comparison_operator: "≥", value: { var: "start.min" } },
        ],
      },
    });
    const [noVar, noValue, byVar] = values.metadata_filter.conditions;
    // 변수 값 조건은 괜찮다. 상수로 바꿔 숫자가 아닌 값을 넣으면 숫자 필드라 막는다.
    const text = { ...byVar!, source: "const" as const, value_text: "올해" };
    const withConditions = (conditions: (typeof values.metadata_filter.conditions)[number][]) => ({
      ...values,
      metadata_filter: { ...values.metadata_filter, conditions },
    });
    expect(schema.safeParse(values).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "metadata_filter.conditions.0.value",
      "metadata_filter.conditions.1.value",
    ]);
    const issues = schema.safeParse(withConditions([noVar!, noValue!, text])).error!.issues;
    expect(issues.map((issue) => [issue.path.join("."), issue.message])).toEqual([
      ["metadata_filter.conditions.0.value", "변수를 고르세요"],
      ["metadata_filter.conditions.1.value", "필수 항목입니다"],
      ["metadata_filter.conditions.2.value", "숫자를 입력하세요"],
    ]);
    // 필터를 끄면 조건을 보지 않지만 config에는 남아 다시 켜면 그대로다.
    const off = withConditions([noVar!, noValue!, text]);
    off.metadata_filter.mode = "disabled";
    expect(schema.safeParse(off).success).toBe(true);
    expect(knowledgeRetrievalFromForm(off).metadata_filter.conditions).toHaveLength(3);
    // 저장된 값만으로는 글로 짐작했던 조건도 필드 타입(number)으로 보낸다.
    const year = withConditions([{ ...text, value_text: "2024" }]);
    expect(knowledgeRetrievalFromForm(year, fields).metadata_filter.conditions).toEqual([
      { name: "year", comparison_operator: "≥", value: 2024 },
    ]);
  });

  it("llm 컨텍스트 선택기는 서버 CONTEXT_TYPES와 같은 변수만 맞게 본다", () => {
    const block = /CONTEXT_TYPES = frozenset\(\{([^}]*)\}\)/.exec(llmSource)?.[1] ?? "";
    const server = [...block.matchAll(/"([^"]+)"/g)].map((match) => match[1]);
    expect([...CONTEXT_TYPES].sort()).toEqual(server.sort());
    const fits = VAR_TYPES.filter((type) =>
      CONTEXT_ACCEPT.some((target) => assignable(type, target) !== "no"),
    );
    expect([...fits].sort()).toEqual([...CONTEXT_TYPES].sort());
  });

  it("지식 쓰기는 고른 쪽 변수만 보내고 메타데이터 이름이 겹치면 막는다", () => {
    const schema = knowledgeIndexSchema(t, tw, DEFAULT_KNOWLEDGE_LIMITS);
    const values = knowledgeIndexToForm({
      kb_id: KB_ID,
      source: { text_var: "llm_1.text", files_var: null },
      metadata: [
        { name: "topic", value: "a" },
        { name: "topic", value: "b" },
      ],
      timeout_sec: 5000,
    });
    expect(schema.safeParse(values).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "metadata.1.name",
      "timeout_sec",
    ]);
    const files = knowledgeIndexFromForm({
      ...values,
      source_kind: "files",
      files_var: "start.docs",
    });
    expect(files.source).toEqual({ text_var: null, files_var: "start.docs" });
    const missing = schema.safeParse({
      ...values,
      source_kind: "files",
      metadata: [],
      timeout_sec: 60,
    });
    expect(missing.error!.issues[0]?.path).toEqual(["files_var"]);
  });

  it("llm 컨텍스트는 켜고 변수를 고른 때만 보낸다", () => {
    const values = llmToForm({
      prompt: [{ role: "user", text: "q" }],
      context: { var: "k.result" },
    });
    expect(values.context).toEqual({ enabled: true, var: "k.result" });
    const off = llmFromForm({ ...values, context: { enabled: false, var: "k.result" } });
    expect(off.context).toBeNull();
    const result = llmSchema(t, tw).safeParse({ ...values, context: { enabled: true, var: "" } });
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual(["context.var"]);
  });

  it("SQL 스키마는 데이터 소스를 골라야 하고 토큰 상한은 500~64000이다", () => {
    const schema = sqlSchemaSchema(t, tw);
    const empty = sqlSchemaToForm(defaultConfig("sql_schema", "chatflow", labels));
    expect(empty).toEqual({ source_id: "", tables: [], include_samples: true, max_tokens: 8000 });
    const issues = schema.safeParse({ ...empty, max_tokens: 100 }).error!.issues;
    expect(issues.map((issue) => [issue.path.join("."), issue.message])).toEqual([
      ["source_id", "데이터 소스를 고르세요."],
      ["max_tokens", "500 이상이어야 합니다"],
    ]);
    expect(sqlSchemaFromForm({ ...empty, source_id: SOURCE_ID }).source_id).toBe(SOURCE_ID);
    expect(sqlSchemaFromForm(empty).source_id).toBeNull();
  });

  it("SQL 실행은 템플릿이나 변수 중 고른 쪽이 있어야 하고, 비운 한도·승인은 null로 데이터 소스 값을 따른다", () => {
    const schema = sqlQuerySchema(t, tw, { maxRowsMax: 1000, timeoutMax: 60, llmBytesMax: 65536 });
    const empty = sqlQueryToForm(defaultConfig("sql_query", "chatflow", labels));
    expect(empty).toMatchObject({ sql_mode: "template", approval: "inherit", max_rows: null });
    expect(schema.safeParse(empty).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "source_id",
      "sql",
    ]);
    const byVar = { ...empty, source_id: SOURCE_ID, sql_mode: "var" as const, sql_var: "llm 1" };
    expect(schema.safeParse(byVar).error!.issues.map((issue) => issue.message)).toEqual([
      "변수 선택자 형식이 아닙니다",
    ]);
    // 행 상한은 실행 상한(maxRowsMax 1000)이 아니라 서버 SqlQueryConfig처럼 10,000까지 받는다.
    const withVar = { ...byVar, sql_var: "llm_1.text" };
    expect(schema.safeParse({ ...withVar, max_rows: SQL_QUERY_MAX_ROWS }).success).toBe(true);
    expect(
      problems("sql_query", sqlQueryFromForm({ ...withVar, max_rows: SQL_QUERY_MAX_ROWS })),
    ).toEqual([]);
    expect(
      problems("sql_query", sqlQueryFromForm({ ...withVar, max_rows: SQL_QUERY_MAX_ROWS + 1 })),
    ).not.toEqual([]);
    const tooMany = { ...withVar, max_rows: SQL_QUERY_MAX_ROWS + 1, timeout_sec: 90 };
    expect(schema.safeParse(tooMany).error!.issues.map((issue) => issue.path.join("."))).toEqual([
      "max_rows",
      "timeout_sec",
    ]);
    // 변수 쪽이면 sql_var만 쓰이고 템플릿 글은 남겨 둔다. 상속은 null이다.
    expect(sqlQueryFromForm({ ...byVar, sql: "SELECT 1", sql_var: "llm_1.text" })).toEqual({
      source_id: SOURCE_ID,
      sql: "SELECT 1",
      sql_var: "llm_1.text",
      max_rows: null,
      timeout_sec: null,
      approval: null,
      llm_rows: null,
      llm_bytes: null,
      export_file: true,
    });
    expect(sqlQueryFromForm({ ...byVar, sql_mode: "template" }).sql_var).toBeNull();
    // 승인 선택지는 상속(null) + 서버 enum이다.
    for (const value of SQL_APPROVALS) {
      const sent = sqlQueryFromForm({ ...byVar, sql_var: "llm_1.text", approval: value });
      expect(problems("sql_query", sent)).toEqual([]);
    }
    expect(problems("sql_query", { approval: "inherit" })).not.toEqual([]);
  });

  it("human_input은 필드가 하나 이상이어야 한다", () => {
    const result = humanInputSchema(t, tw).safeParse({ description: "", fields: [] });
    expect(result.error!.issues.map((issue) => issue.path.join("."))).toEqual(["fields"]);
  });
});
