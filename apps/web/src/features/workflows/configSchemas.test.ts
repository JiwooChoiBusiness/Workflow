/*
 * 노드 폼의 config가 서버 Config 모델(GET /api/console/node-types의 config_schema)을 지키는지 본다.
 * 픽스처는 LLM_FAKE=true 서버에서 받은 응답이다(2026-10-14, M14까지). 서버 노드 config가 바뀌면
 * 픽스처를 다시 받고 이 테스트가 어긋난 곳을 알려 준다.
 */
import { describe, expect, it } from "vitest";
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
  assignerToForm,
  classifierFromForm,
  classifierSchema,
  classifierToForm,
  codeFromForm,
  codeToForm,
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
  IF_ELSE_OPS,
  ifElseFromForm,
  ifElseSchema,
  ifElseToForm,
  ITERATION_ERROR_MODES,
  iterationFromForm,
  iterationToForm,
  llmFromForm,
  llmSchema,
  llmToForm,
  opsFor,
  PARAM_TYPES,
  PROMPT_ROLES,
  START_FIELD_TYPES,
  startFromForm,
  startSchema,
  startToForm,
  templateFromForm,
  templateToForm,
  VAR_TYPE_OPTIONS,
} from "./configSchemas";
import { DEFAULT_SYSTEM_PROMPT, defaultConfig, PALETTE_ORDER } from "./nodeTypes";

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

function enumAt(type: string, ...path: string[]): unknown[] {
  const root = schemas.get(type)!;
  let current: Schema = root;
  for (const key of path) {
    current = resolve(current, root);
    if (key === "[]") current = current.items as Schema;
    else current = (current.properties as Record<string, Schema>)[key]!;
  }
  current = resolve(current, root);
  return (current.enum ?? []) as unknown[];
}

const labels = { className: (n: number) => `분류 ${n}` };
const AGENT_ID = "01a1391c-6458-7b30-9a21-310217cd8e29";

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
    },
    (c) => llmFromForm(llmToForm(c)),
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
};

describe("서버 config_schema 계약", () => {
  it("픽스처의 모든 타입에 폼이나 자리가 있다", () => {
    const types = nodeTypeList.items
      .map((item) => item.type)
      .filter((type) => type !== "iteration_start");
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
});
