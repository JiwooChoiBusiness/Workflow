/*
 * 노드 속성 폼의 zod 스키마와 config ↔ 폼 값 변환(설계 7.3: JSON Schema에서 만들지 않고 타입별로 손으로
 * 쓴다). 범위·패턴·열거값은 서버 Config 모델(GET /api/console/node-types의 config_schema)과 같게 두고,
 * 변환 결과가 그 스키마를 통과하는지는 configSchemas.test.ts가 서버 응답 픽스처로 확인한다.
 *
 * 폼 값이 config와 다른 곳: 목록 글(선택지·enum·in 값)은 줄·쉼표로 쓴 글이고, JSON 칸(출력 스키마)은
 * 글이다. 고친 값은 검증과 관계없이 바로 그래프에 들어간다. 초안은 깨진 채로도 저장되고 서버 검증이
 * 최종 판정이기 때문이다.
 */
import type { TFunction } from "i18next";
import * as z from "zod";
import type { VarType } from "@/api/types";
import { decimal, integer, type T } from "@/lib/formSchemas";
import { LOOP_COUNT_OUTPUT, parseSelector } from "./graph";
import {
  defaultModel,
  DEFAULT_MAX_OUTPUT_TOKENS,
  HTTP_MAX_BODY_BYTES,
  LOOP_DEFAULT_ITERATIONS,
  LOOP_MAX_ITERATIONS,
} from "./nodeTypes";

export type TW = TFunction<"workflows">;

export const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$/;

export const VAR_TYPE_OPTIONS = [
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
] as const satisfies readonly VarType[];

export const START_FIELD_TYPES = [
  "string",
  "paragraph",
  "number",
  "boolean",
  "select",
  "file",
  "file_list",
] as const;
export type StartFieldType = (typeof START_FIELD_TYPES)[number];

export const IF_ELSE_OPS = [
  "=",
  "≠",
  "contains",
  "not_contains",
  "starts_with",
  "ends_with",
  "is_empty",
  "is_not_empty",
  ">",
  "<",
  "≥",
  "≤",
  "in",
  "not_in",
  "regex",
] as const;
export type IfElseOp = (typeof IF_ELSE_OPS)[number];
// 번역 키로 쓸 이름(기호 연산자는 키로 쓰지 않는다)
export const OP_KEYS = {
  "=": "eq",
  "≠": "ne",
  contains: "contains",
  not_contains: "not_contains",
  starts_with: "starts_with",
  ends_with: "ends_with",
  is_empty: "is_empty",
  is_not_empty: "is_not_empty",
  ">": "gt",
  "<": "lt",
  "≥": "ge",
  "≤": "le",
  in: "in",
  not_in: "not_in",
  regex: "regex",
} as const satisfies Record<IfElseOp, string>;
export const UNARY_OPS: ReadonlySet<string> = new Set(["is_empty", "is_not_empty"]);
export const LIST_OPS: ReadonlySet<string> = new Set(["in", "not_in"]);
const ORDER_OPS: ReadonlySet<string> = new Set([">", "<", "≥", "≤"]);
const TEXT_OPS: ReadonlySet<string> = new Set([
  "contains",
  "not_contains",
  "starts_with",
  "ends_with",
]);

// 서버 if_else._OPS_BY_GROUP. 변수 타입에 맞지 않는 연산자는 검증 오류라 목록에서 뺀다.
const OPS_BY_GROUP: Record<string, readonly IfElseOp[]> = {
  string: [
    "=",
    "≠",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "regex",
    "is_empty",
    "is_not_empty",
  ],
  number: ["=", "≠", "in", "not_in", ">", "<", "≥", "≤", "is_empty", "is_not_empty"],
  boolean: ["=", "≠", "is_empty", "is_not_empty"],
  object: ["is_empty", "is_not_empty"],
  file: ["is_empty", "is_not_empty"],
  array: ["contains", "not_contains", "is_empty", "is_not_empty"],
};

export function opsFor(type: VarType | null | undefined): readonly IfElseOp[] {
  if (!type || type === "any") return IF_ELSE_OPS;
  const group = type.startsWith("array[") ? "array" : type;
  return OPS_BY_GROUP[group] ?? IF_ELSE_OPS;
}

export const ASSIGN_OPS = ["overwrite", "append", "clear", "+=", "-="] as const;
export type AssignOp = (typeof ASSIGN_OPS)[number];
export const ASSIGN_OP_KEYS = {
  overwrite: "overwrite",
  append: "append",
  clear: "clear",
  "+=": "add",
  "-=": "sub",
} as const satisfies Record<AssignOp, string>;

export const PARAM_TYPES = [
  "string",
  "number",
  "boolean",
  "object",
  "array[string]",
  "array[number]",
  "array[boolean]",
  "array[object]",
] as const;

export const HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] as const;
export const HTTP_BODY_TYPES = ["none", "json", "form", "raw", "file"] as const;
export const HTTP_AUTH_TYPES = ["none", "bearer", "basic", "header"] as const;
export const PROMPT_ROLES = ["system", "user", "assistant"] as const;
export const REASONING_EFFORTS = ["minimal", "low", "medium", "high"] as const;
export const ITERATION_ERROR_MODES = ["terminate", "continue", "remove"] as const;

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function num(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function oneOf<V extends string>(values: readonly V[], value: unknown, fallback: V): V {
  return typeof value === "string" && (values as readonly string[]).includes(value)
    ? (value as V)
    : fallback;
}

export function isNumeric(text: string): boolean {
  return text.trim() !== "" && Number.isFinite(Number(text));
}

export function splitLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function splitComma(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

// JSON 칸의 글을 config 값으로. 비면 null, 객체·값으로 읽히면 그 값, 아니면 글 그대로(서버 검증이 알린다).
export function parseJsonText(text: string): unknown {
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function jsonText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

// 대입·기본값 칸의 글을 타입에 맞는 값으로. 글 타입은 글 그대로 두고, 나머지는 JSON으로 읽힐 때만 바꾼다.
export function literalValue(text: string, type: VarType | null | undefined): unknown {
  if (type === "string") return text;
  if (type === "number") return isNumeric(text) ? Number(text) : text;
  if (type === "boolean") {
    const lower = text.trim().toLowerCase();
    return lower === "true" ? true : lower === "false" ? false : text;
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function literalText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

// 이름이 겹치는 두 번째 항목마다 오류
function uniqueBy<K extends string>(key: K, message: string) {
  return (items: Record<K, string>[], ctx: z.RefinementCtx) => {
    const seen = new Set<string>();
    items.forEach((item, index) => {
      const value = item[key].trim();
      if (value && seen.has(value)) {
        ctx.addIssue({ code: "custom", path: [index, key], message });
      }
      seen.add(value);
    });
  };
}

function nameSchema(t: T, tw: TW) {
  return z
    .string()
    .trim()
    .min(1, { error: t("validation.required") })
    .regex(NAME_PATTERN, { error: tw("validation.name") });
}

export function selectorSchema(tw: TW, optional = false) {
  return z.string().superRefine((value, ctx) => {
    const text = value.trim();
    if (!text) {
      if (!optional) ctx.addIssue({ code: "custom", message: tw("validation.selector") });
      return;
    }
    if (!parseSelector(text))
      ctx.addIssue({ code: "custom", message: tw("validation.selectorFormat") });
  });
}

function jsonObjectText(tw: TW, schemaLike: boolean) {
  return z.string().superRefine((value, ctx) => {
    if (!value.trim()) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(value);
    } catch {
      ctx.addIssue({ code: "custom", message: tw("validation.json") });
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      ctx.addIssue({ code: "custom", message: tw("validation.jsonObject") });
    } else if (schemaLike && (parsed as { type?: unknown }).type !== "object") {
      ctx.addIssue({ code: "custom", message: tw("validation.schemaObject") });
    }
  });
}

function nameVarItems(t: T, tw: TW) {
  return z
    .array(z.object({ name: nameSchema(t, tw), var: selectorSchema(tw) }))
    .superRefine(uniqueBy("name", t("validation.duplicate")));
}

function nameVarToForm(value: unknown) {
  return records(value).map((item) => ({ name: str(item.name), var: str(item.var) }));
}

function nameVarFromForm(items: { name: string; var: string }[]) {
  return items.map((item) => ({ name: item.name.trim(), var: item.var.trim() }));
}

function modelSchema(t: T) {
  return z.object({
    model_id: z.string().nullable(),
    params: z.object({
      temperature: decimal(t, 0, 2).nullable(),
      // 서버는 0을 받지 않는다(0 < top_p ≤ 1).
      top_p: z
        .number({ error: t("validation.number") })
        .gt(0, { error: t("validation.positive") })
        .max(1, { error: t("validation.max", { max: 1 }) })
        .nullable(),
      max_output_tokens: integer(t, 1),
      reasoning_effort: z.enum(REASONING_EFFORTS).nullable(),
    }),
  });
}

export interface ModelForm {
  model_id: string | null;
  params: {
    temperature: number | null;
    top_p: number | null;
    max_output_tokens: number;
    reasoning_effort: (typeof REASONING_EFFORTS)[number] | null;
  };
}

function modelToForm(value: unknown): ModelForm {
  const model = record(value);
  const params = record(model.params);
  const fallback = defaultModel();
  return {
    model_id: typeof model.model_id === "string" && model.model_id ? model.model_id : null,
    params: {
      temperature: numOrNull(params.temperature),
      top_p: numOrNull(params.top_p),
      max_output_tokens: num(params.max_output_tokens, fallback.params.max_output_tokens),
      reasoning_effort: (REASONING_EFFORTS as readonly unknown[]).includes(params.reasoning_effort)
        ? (params.reasoning_effort as ModelForm["params"]["reasoning_effort"])
        : null,
    },
  };
}

function modelFromForm(model: ModelForm) {
  return {
    model_id: model.model_id?.trim() || null,
    params: {
      temperature: model.params.temperature,
      top_p: model.params.top_p,
      max_output_tokens: Number.isFinite(model.params.max_output_tokens)
        ? model.params.max_output_tokens
        : DEFAULT_MAX_OUTPUT_TOKENS,
      reasoning_effort: model.params.reasoning_effort,
    },
  };
}

// start

// start 필드 목록. human_input 폼 필드도 같은 모양이라 함께 쓴다(서버 StartField).
function startFieldsSchema(t: T, tw: TW, min = 0) {
  return z
    .array(
      z
        .object({
          name: nameSchema(t, tw),
          type: z.enum(START_FIELD_TYPES),
          label: z.string(),
          required: z.boolean(),
          default_text: z.string(),
          options_text: z.string(),
          max_length: integer(t, 1).nullable(),
        })
        .superRefine((field, ctx) => {
          if (field.type === "select" && splitLines(field.options_text).length === 0) {
            ctx.addIssue({
              code: "custom",
              path: ["options_text"],
              message: tw("validation.options"),
            });
          }
          if (
            field.type === "number" &&
            field.default_text.trim() &&
            !isNumeric(field.default_text)
          ) {
            ctx.addIssue({
              code: "custom",
              path: ["default_text"],
              message: tw("validation.number"),
            });
          }
        }),
    )
    .min(min, { error: tw("validation.list") })
    .superRefine(uniqueBy("name", t("validation.duplicate")));
}

export function startSchema(t: T, tw: TW) {
  return z.object({ fields: startFieldsSchema(t, tw) });
}

export type StartForm = z.infer<ReturnType<typeof startSchema>>;
export type StartFieldForm = StartForm["fields"][number];

export function startToForm(config: Record<string, unknown>): StartForm {
  return {
    fields: records(config.fields).map((field) => ({
      name: str(field.name),
      type: oneOf(START_FIELD_TYPES, field.type, "string"),
      label: str(field.label),
      required: bool(field.required),
      default_text: literalText(field.default),
      options_text: Array.isArray(field.options) ? field.options.map(String).join("\n") : "",
      max_length: numOrNull(field.max_length),
    })),
  };
}

function startDefault(field: StartFieldForm): unknown {
  const text = field.default_text;
  if (!text.trim() || field.type === "file" || field.type === "file_list") return null;
  if (field.type === "number") return isNumeric(text) ? Number(text) : text;
  if (field.type === "boolean") return text === "true" ? true : text === "false" ? false : null;
  return text;
}

export function startFromForm(values: StartForm): Record<string, unknown> {
  return {
    fields: values.fields.map((field) => ({
      name: field.name.trim(),
      type: field.type,
      label: field.label,
      required: field.required,
      default: startDefault(field),
      options: field.type === "select" ? splitLines(field.options_text) : null,
      max_length: field.type === "string" || field.type === "paragraph" ? field.max_length : null,
    })),
  };
}

export function newStartField(): StartFieldForm {
  return {
    name: "",
    type: "string",
    label: "",
    required: false,
    default_text: "",
    options_text: "",
    max_length: null,
  };
}

// end

export function endSchema(t: T, tw: TW) {
  return z.object({ outputs: nameVarItems(t, tw) });
}
export type EndForm = z.infer<ReturnType<typeof endSchema>>;
export const endToForm = (config: Record<string, unknown>): EndForm => ({
  outputs: nameVarToForm(config.outputs),
});
export const endFromForm = (values: EndForm) => ({ outputs: nameVarFromForm(values.outputs) });

// answer

export function answerSchema() {
  return z.object({ text: z.string() });
}
export type AnswerForm = z.infer<ReturnType<typeof answerSchema>>;
export const answerToForm = (config: Record<string, unknown>): AnswerForm => ({
  text: str(config.text),
});
export const answerFromForm = (values: AnswerForm) => ({ text: values.text });

// template

export function templateSchema(t: T, tw: TW) {
  return z.object({ template: z.string(), vars: nameVarItems(t, tw) });
}
export type TemplateForm = z.infer<ReturnType<typeof templateSchema>>;
export const templateToForm = (config: Record<string, unknown>): TemplateForm => ({
  template: str(config.template),
  vars: nameVarToForm(config.vars),
});
export const templateFromForm = (values: TemplateForm) => ({
  template: values.template,
  vars: nameVarFromForm(values.vars),
});

// if_else

// 연산자에 맞는 비교 값인지(if_else 조건, loop 종료 조건, list_operator 필터가 같이 쓴다).
function checkConditionValue(
  t: T,
  tw: TW,
  item: { op: IfElseOp; value_text: string },
  ctx: z.RefinementCtx,
) {
  const needsValue = ORDER_OPS.has(item.op) || TEXT_OPS.has(item.op) || item.op === "regex";
  if (needsValue && !item.value_text.trim()) {
    ctx.addIssue({ code: "custom", path: ["value_text"], message: t("validation.required") });
  }
  if (ORDER_OPS.has(item.op) && item.value_text.trim() && !isNumeric(item.value_text)) {
    ctx.addIssue({ code: "custom", path: ["value_text"], message: tw("validation.number") });
  }
  if (item.op === "regex" && item.value_text.length > 512) {
    ctx.addIssue({
      code: "custom",
      path: ["value_text"],
      message: t("validation.maxLength", { max: 512 }),
    });
  }
}

function conditionSchema(t: T, tw: TW) {
  return z
    .object({ var: selectorSchema(tw), op: z.enum(IF_ELSE_OPS), value_text: z.string() })
    .superRefine((item, ctx) => checkConditionValue(t, tw, item, ctx));
}

export function ifElseSchema(t: T, tw: TW) {
  const condition = conditionSchema(t, tw);
  return z.object({
    cases: z
      .array(
        z.object({
          id: z.string().regex(/^[A-Za-z0-9_]{1,32}$/),
          logical: z.enum(["and", "or"]),
          conditions: z.array(condition).min(1, { error: tw("validation.list") }),
        }),
      )
      .min(1, { error: tw("validation.list") })
      .superRefine(uniqueBy("id", t("validation.duplicate"))),
  });
}
export type IfElseForm = z.infer<ReturnType<typeof ifElseSchema>>;
export type ConditionForm = IfElseForm["cases"][number]["conditions"][number];

export function conditionValueText(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ");
  return literalText(value);
}

export function conditionValue(op: string, text: string): unknown {
  if (UNARY_OPS.has(op)) return null;
  if (LIST_OPS.has(op)) return splitComma(text);
  return text;
}

function conditionToForm(condition: Record<string, unknown>): ConditionForm {
  return {
    var: str(condition.var),
    op: oneOf(IF_ELSE_OPS, condition.op, "="),
    value_text: conditionValueText(condition.value),
  };
}

function conditionFromForm(condition: ConditionForm) {
  return {
    var: condition.var.trim(),
    op: condition.op,
    value: conditionValue(condition.op, condition.value_text),
  };
}

export function ifElseToForm(config: Record<string, unknown>): IfElseForm {
  return {
    cases: records(config.cases).map((item, index) => ({
      id: str(item.id, String(index + 1)) || String(index + 1),
      logical: item.logical === "or" ? "or" : "and",
      conditions: records(item.conditions).map(conditionToForm),
    })),
  };
}

export function ifElseFromForm(values: IfElseForm) {
  return {
    cases: values.cases.map((item) => ({
      id: item.id,
      logical: item.logical,
      conditions: item.conditions.map(conditionFromForm),
    })),
  };
}

export function newCondition(): ConditionForm {
  return { var: "", op: "=", value_text: "" };
}

// variable_aggregator

export function aggregatorSchema(tw: TW) {
  return z.object({
    vars: z.array(z.object({ var: selectorSchema(tw) })).min(1, { error: tw("validation.list") }),
    output_type: z.enum(VAR_TYPE_OPTIONS),
  });
}
export type AggregatorForm = z.infer<ReturnType<typeof aggregatorSchema>>;
export const aggregatorToForm = (config: Record<string, unknown>): AggregatorForm => ({
  vars: (Array.isArray(config.vars) ? config.vars : []).map((item) => ({ var: str(item) })),
  output_type: oneOf(VAR_TYPE_OPTIONS, config.output_type, "any"),
});
export const aggregatorFromForm = (values: AggregatorForm) => ({
  vars: values.vars.map((item) => item.var.trim()),
  output_type: values.output_type,
});

// variable_assigner

// 대입 대상으로 고를 수 있는 것. loop 안이면 그 loop의 반복 변수(<loop id>.<이름>)도, 대화 변수는 챗플로우만.
export interface AssignTargets {
  loopId: string | null;
  conv: boolean;
}

function assignTargetOk(target: string, targets: AssignTargets): boolean {
  const selector = parseSelector(target);
  if (!selector || selector.path) return false;
  if (selector.scope === "conv") return targets.conv;
  return targets.loopId !== null && selector.scope === targets.loopId;
}

export function assignerSchema(tw: TW, targets: AssignTargets = { loopId: null, conv: true }) {
  return z.object({
    items: z
      .array(
        z
          .object({
            target: z.string(),
            op: z.enum(ASSIGN_OPS),
            source: z.enum(["var", "value"]),
            var: z.string(),
            value_text: z.string(),
          })
          .superRefine((item, ctx) => {
            if (!assignTargetOk(item.target, targets)) {
              ctx.addIssue({
                code: "custom",
                path: ["target"],
                message:
                  targets.loopId === null
                    ? tw("validation.convTarget")
                    : tw("validation.assignTarget"),
              });
            }
            if (item.op === "clear") return;
            if (item.source === "var") {
              if (!item.var.trim()) {
                ctx.addIssue({ code: "custom", path: ["var"], message: tw("validation.selector") });
              } else if (!parseSelector(item.var)) {
                ctx.addIssue({
                  code: "custom",
                  path: ["var"],
                  message: tw("validation.selectorFormat"),
                });
              }
            } else if ((item.op === "+=" || item.op === "-=") && !isNumeric(item.value_text)) {
              ctx.addIssue({
                code: "custom",
                path: ["value_text"],
                message: tw("validation.number"),
              });
            }
          }),
      )
      .min(1, { error: tw("validation.list") }),
  });
}
export type AssignerForm = z.infer<ReturnType<typeof assignerSchema>>;
export type AssignItemForm = AssignerForm["items"][number];

export function assignerToForm(config: Record<string, unknown>): AssignerForm {
  return {
    items: records(config.items).map((item) => {
      const hasVar = typeof item.var === "string";
      return {
        target: str(item.target),
        op: oneOf(ASSIGN_OPS, item.op, "overwrite"),
        source: hasVar || !("value" in item) ? "var" : "value",
        var: str(item.var),
        value_text: literalText(item.value),
      };
    }),
  };
}

const ARRAY_ITEM: Record<string, VarType> = {
  "array[string]": "string",
  "array[number]": "number",
  "array[boolean]": "boolean",
  "array[object]": "object",
  "array[file]": "file",
  "array[any]": "any",
};

/*
 * 서버는 value 키가 있기만 해도 고정값으로 본다(model_fields_set). 변수를 쓸 때는 value를 넣지 않는다. 고정값은
 * 대상의 선언 타입(대화 변수, loop 안이면 반복 변수)에 맞춰 읽는다.
 */
export function assignerFromForm(
  values: AssignerForm,
  convTypes: Record<string, VarType>,
  loop: { id: string; types: Record<string, VarType> } | null = null,
) {
  return {
    items: values.items.map((item) => {
      const target = item.target.trim();
      if (item.op === "clear") return { target, op: item.op };
      if (item.source === "var") return { target, op: item.op, var: item.var.trim() };
      const selector = parseSelector(target);
      const types = loop && selector?.scope === loop.id ? loop.types : convTypes;
      const declared = types[selector?.name ?? ""];
      let type: VarType | undefined = declared;
      if (item.op === "append" && declared) type = ARRAY_ITEM[declared] ?? "any";
      if (item.op === "+=" || item.op === "-=") type = "number";
      return { target, op: item.op, value: literalValue(item.value_text, type) };
    }),
  };
}

// llm

export function llmSchema(t: T, tw: TW) {
  return z
    .object({
      model: modelSchema(t),
      prompt: z.array(z.object({ role: z.enum(PROMPT_ROLES), text: z.string() })),
      memory: z.object({ enabled: z.boolean(), window_turns: integer(t, 1, 50) }),
      vision: z.object({ enabled: z.boolean(), var: z.string() }),
      json_mode: z.boolean(),
      output_schema_text: jsonObjectText(tw, true),
    })
    .superRefine((values, ctx) => {
      if (!values.prompt.some((item) => item.role === "user")) {
        ctx.addIssue({ code: "custom", path: ["prompt"], message: tw("validation.promptUser") });
      }
      if (values.vision.enabled) {
        const text = values.vision.var.trim();
        if (!text || !parseSelector(text)) {
          ctx.addIssue({
            code: "custom",
            path: ["vision", "var"],
            message: tw("validation.selector"),
          });
        }
      }
    });
}
export type LlmForm = z.infer<ReturnType<typeof llmSchema>>;

export function llmToForm(config: Record<string, unknown>): LlmForm {
  const memory = record(config.memory);
  const vision = record(config.vision);
  return {
    model: modelToForm(config.model),
    prompt: records(config.prompt).map((item) => ({
      role: oneOf(PROMPT_ROLES, item.role, "user"),
      text: str(item.text),
    })),
    memory: { enabled: bool(memory.enabled), window_turns: num(memory.window_turns, 10) },
    vision: { enabled: bool(vision.enabled), var: str(vision.var) },
    json_mode: bool(config.json_mode),
    output_schema_text: jsonText(config.output_schema),
  };
}

export function llmFromForm(values: LlmForm) {
  return {
    model: modelFromForm(values.model),
    prompt: values.prompt.map((item) => ({ role: item.role, text: item.text })),
    memory: { enabled: values.memory.enabled, window_turns: values.memory.window_turns },
    vision: { enabled: values.vision.enabled, var: values.vision.var.trim() || null },
    json_mode: values.json_mode,
    output_schema: parseJsonText(values.output_schema_text),
  };
}

// http_request

function keyValues(t: T, header: boolean, tw: TW) {
  return z.array(
    z.object({
      name: header
        ? z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .regex(HEADER_NAME, { error: tw("validation.headerName") })
        : z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .max(256, { error: t("validation.maxLength", { max: 256 }) }),
      value: z.string(),
    }),
  );
}

export function httpSchema(t: T, tw: TW) {
  return z
    .object({
      method: z.enum(HTTP_METHODS),
      url: z
        .string()
        .trim()
        .min(1, { error: t("validation.required") }),
      params: keyValues(t, false, tw),
      headers: keyValues(t, true, tw),
      body_type: z.enum(HTTP_BODY_TYPES),
      body_text: z.string(),
      body_form: keyValues(t, false, tw),
      auth: z.object({
        type: z.enum(HTTP_AUTH_TYPES),
        value: z.string(),
        header: z.string(),
      }),
      timeout_sec: integer(t, 1, 300),
      max_body_bytes: integer(t, 1, HTTP_MAX_BODY_BYTES),
    })
    .superRefine((values, ctx) => {
      if (values.auth.type === "header" && !HEADER_NAME.test(values.auth.header)) {
        ctx.addIssue({
          code: "custom",
          path: ["auth", "header"],
          message: tw("validation.headerName"),
        });
      }
    });
}
export type HttpForm = z.infer<ReturnType<typeof httpSchema>>;

function kvToForm(value: unknown) {
  return records(value).map((item) => ({ name: str(item.name), value: str(item.value) }));
}

export function httpToForm(config: Record<string, unknown>): HttpForm {
  const body = record(config.body);
  const auth = record(config.auth);
  return {
    method: oneOf(HTTP_METHODS, config.method, "GET"),
    url: str(config.url),
    params: kvToForm(config.params),
    headers: kvToForm(config.headers),
    body_type: oneOf(HTTP_BODY_TYPES, body.type, "none"),
    body_text: typeof body.content === "string" ? body.content : "",
    body_form: kvToForm(body.content),
    auth: {
      type: oneOf(HTTP_AUTH_TYPES, auth.type, "none"),
      value: str(auth.value),
      header: str(auth.header),
    },
    timeout_sec: num(config.timeout_sec, 60),
    max_body_bytes: num(config.max_body_bytes, HTTP_MAX_BODY_BYTES),
  };
}

export function httpFromForm(values: HttpForm) {
  // form 본문은 이름·값 목록, json·raw는 글이다(서버 HttpBody). 다른 형식의 내용은 보내지 않는다.
  let content: unknown = "";
  if (values.body_type === "form") content = values.body_form;
  else if (values.body_type === "json" || values.body_type === "raw") content = values.body_text;
  return {
    method: values.method,
    url: values.url,
    headers: values.headers,
    params: values.params,
    body: { type: values.body_type, content },
    auth: values.auth,
    timeout_sec: values.timeout_sec,
    max_body_bytes: values.max_body_bytes,
  };
}

// code

export function codeSchema(t: T, tw: TW) {
  return z.object({
    code: z.string(),
    inputs: nameVarItems(t, tw),
    outputs: z
      .array(z.object({ name: nameSchema(t, tw), type: z.enum(VAR_TYPE_OPTIONS) }))
      .superRefine(uniqueBy("name", t("validation.duplicate"))),
    timeout_sec: integer(t, 1, 60).nullable(),
  });
}
export type CodeForm = z.infer<ReturnType<typeof codeSchema>>;
export const codeToForm = (config: Record<string, unknown>): CodeForm => ({
  code: str(config.code),
  inputs: nameVarToForm(config.inputs),
  outputs: records(config.outputs).map((item) => ({
    name: str(item.name),
    type: oneOf(VAR_TYPE_OPTIONS, item.type, "any"),
  })),
  timeout_sec: numOrNull(config.timeout_sec),
});
export const codeFromForm = (values: CodeForm) => ({
  code: values.code,
  inputs: nameVarFromForm(values.inputs),
  outputs: values.outputs.map((item) => ({ name: item.name.trim(), type: item.type })),
  timeout_sec: values.timeout_sec,
});

// question_classifier

export function classifierSchema(t: T, tw: TW) {
  return z.object({
    model: modelSchema(t),
    query_var: selectorSchema(tw),
    classes: z
      .array(
        z.object({
          id: z.string().regex(/^[A-Za-z0-9_]{1,32}$/),
          name: z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .max(200, { error: t("validation.maxLength", { max: 200 }) }),
          description: z.string().max(2000, { error: t("validation.maxLength", { max: 2000 }) }),
        }),
      )
      .min(1, { error: tw("validation.list") })
      .max(50, { error: t("validation.maxItems", { max: 50 }) })
      .superRefine(uniqueBy("id", t("validation.duplicate"))),
    instruction: z.string(),
    on_parse_failure: z.enum(["first_class", "fail"]),
  });
}
export type ClassifierForm = z.infer<ReturnType<typeof classifierSchema>>;
export const classifierToForm = (config: Record<string, unknown>): ClassifierForm => ({
  model: modelToForm(config.model),
  query_var: str(config.query_var),
  classes: records(config.classes).map((item, index) => ({
    id: str(item.id, String(index + 1)) || String(index + 1),
    name: str(item.name),
    description: str(item.description),
  })),
  instruction: str(config.instruction),
  on_parse_failure: config.on_parse_failure === "fail" ? "fail" : "first_class",
});
export const classifierFromForm = (values: ClassifierForm) => ({
  model: modelFromForm(values.model),
  query_var: values.query_var.trim(),
  classes: values.classes.map((item) => ({
    id: item.id,
    name: item.name,
    description: item.description,
  })),
  instruction: values.instruction,
  on_parse_failure: values.on_parse_failure,
});

// parameter_extractor

export function extractorSchema(t: T, tw: TW) {
  return z.object({
    model: modelSchema(t),
    query_var: selectorSchema(tw),
    parameters: z
      .array(
        z
          .object({
            name: nameSchema(t, tw).refine((value) => !value.startsWith("__"), {
              error: tw("forms.extractor.reserved"),
            }),
            type: z.enum(PARAM_TYPES),
            description: z.string().max(2000, { error: t("validation.maxLength", { max: 2000 }) }),
            required: z.boolean(),
            enum_text: z.string(),
          })
          .superRefine((item, ctx) => {
            if (!item.enum_text.trim()) return;
            if (item.type !== "string" && item.type !== "number") {
              ctx.addIssue({
                code: "custom",
                path: ["enum_text"],
                message: tw("forms.extractor.enumHint"),
              });
            } else if (item.type === "number" && !splitComma(item.enum_text).every(isNumeric)) {
              ctx.addIssue({
                code: "custom",
                path: ["enum_text"],
                message: tw("validation.number"),
              });
            }
          }),
      )
      .min(1, { error: tw("validation.list") })
      .max(50, { error: t("validation.maxItems", { max: 50 }) })
      .superRefine(uniqueBy("name", t("validation.duplicate"))),
    instruction: z.string(),
    reasoning_mode: z.enum(["tool_call", "prompt"]),
  });
}
export type ExtractorForm = z.infer<ReturnType<typeof extractorSchema>>;
export type ExtractParamForm = ExtractorForm["parameters"][number];

export const extractorToForm = (config: Record<string, unknown>): ExtractorForm => ({
  model: modelToForm(config.model),
  query_var: str(config.query_var),
  parameters: records(config.parameters).map((item) => ({
    name: str(item.name),
    type: oneOf(PARAM_TYPES, item.type, "string"),
    description: str(item.description),
    required: bool(item.required),
    enum_text: Array.isArray(item.enum) ? item.enum.map(String).join(", ") : "",
  })),
  instruction: str(config.instruction),
  reasoning_mode: config.reasoning_mode === "prompt" ? "prompt" : "tool_call",
});

export const extractorFromForm = (values: ExtractorForm) => ({
  model: modelFromForm(values.model),
  query_var: values.query_var.trim(),
  parameters: values.parameters.map((item) => {
    const parts = splitComma(item.enum_text);
    return {
      name: item.name.trim(),
      type: item.type,
      description: item.description,
      required: item.required,
      enum:
        parts.length === 0
          ? null
          : item.type === "number"
            ? parts.map((part) => (isNumeric(part) ? Number(part) : part))
            : parts,
    };
  }),
  instruction: values.instruction,
  reasoning_mode: values.reasoning_mode,
});

export function newExtractParam(): ExtractParamForm {
  return { name: "", type: "string", description: "", required: false, enum_text: "" };
}

// iteration

export function iterationSchema(t: T, tw: TW) {
  return z.object({
    input_var: selectorSchema(tw),
    output_var: selectorSchema(tw),
    parallel: z.boolean(),
    max_parallel: integer(t, 1, 4),
    error_mode: z.enum(ITERATION_ERROR_MODES),
  });
}
export type IterationForm = z.infer<ReturnType<typeof iterationSchema>>;
export const iterationToForm = (config: Record<string, unknown>): IterationForm => ({
  input_var: str(config.input_var),
  output_var: str(config.output_var),
  parallel: bool(config.parallel),
  max_parallel: num(config.max_parallel, 1),
  error_mode: oneOf(ITERATION_ERROR_MODES, config.error_mode, "terminate"),
});
export const iterationFromForm = (values: IterationForm) => ({
  input_var: values.input_var.trim(),
  output_var: values.output_var.trim(),
  parallel: values.parallel,
  max_parallel: values.max_parallel,
  error_mode: values.error_mode,
});

// agent (설계 2.3·12장, 서버 AgentConfig). limits는 준 필드만 에이전트 버전의 한도를 덮어쓴다.

export const AGENT_LIMIT_FIELDS = [
  "max_steps",
  "max_run_seconds",
  "max_total_tokens",
  "tool_timeout_sec",
  "max_cost_usd",
  "context_budget_tokens",
  "max_child_calls",
] as const;
export type AgentLimitField = (typeof AGENT_LIMIT_FIELDS)[number];

// 서버 RunLimits(apps/api/app/schemas/common.py)와 같은 범위. 비운 칸(null)은 보내지 않는다.
function agentLimitsSchema(t: T) {
  return z.object({
    max_steps: integer(t, 1, 200).nullable(),
    max_run_seconds: integer(t, 60, 7200).nullable(),
    max_total_tokens: integer(t, 1).nullable(),
    tool_timeout_sec: integer(t, 1, 900).nullable(),
    max_cost_usd: z
      .number({ error: t("validation.number") })
      .gt(0, { error: t("validation.positive") })
      .nullable(),
    context_budget_tokens: integer(t, 1).nullable(),
    max_child_calls: integer(t, 0, 100).nullable(),
  });
}

export function agentSchema(t: T, tw: TW) {
  return z.object({
    agent_id: z.string().min(1, { error: t("validation.required") }),
    agent_version_id: z.string(),
    instruction: z.string(),
    inputs: nameVarItems(t, tw),
    files_var: selectorSchema(tw, true),
    result_format: z.enum(["text", "json"]),
    result_schema_text: jsonObjectText(tw, true),
    result_files: z.boolean(),
    approval_mode: z.enum(["inherit", "off"]),
    session_mode: z.enum(["ephemeral", "per_conversation"]),
    limits_enabled: z.boolean(),
    limits: agentLimitsSchema(t),
  });
}
export type AgentNodeForm = z.infer<ReturnType<typeof agentSchema>>;

export function agentToForm(config: Record<string, unknown>): AgentNodeForm {
  const result = record(config.result);
  const limits = record(config.limits);
  return {
    agent_id: str(config.agent_id),
    agent_version_id: str(config.agent_version_id),
    instruction: str(config.instruction),
    inputs: nameVarToForm(config.inputs),
    files_var: str(config.files_var),
    result_format: result.format === "json" ? "json" : "text",
    result_schema_text: jsonText(result.schema),
    result_files: bool(result.files, true),
    approval_mode: config.approval_mode === "off" ? "off" : "inherit",
    session_mode: config.session_mode === "per_conversation" ? "per_conversation" : "ephemeral",
    limits_enabled: typeof config.limits === "object" && config.limits !== null,
    limits: {
      max_steps: numOrNull(limits.max_steps),
      max_run_seconds: numOrNull(limits.max_run_seconds),
      max_total_tokens: numOrNull(limits.max_total_tokens),
      tool_timeout_sec: numOrNull(limits.tool_timeout_sec),
      max_cost_usd: numOrNull(limits.max_cost_usd),
      context_budget_tokens: numOrNull(limits.context_budget_tokens),
      max_child_calls: numOrNull(limits.max_child_calls),
    },
  };
}

function agentLimits(values: AgentNodeForm): Record<string, number> | null {
  if (!values.limits_enabled) return null;
  const given = Object.entries(values.limits).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number" && !Number.isNaN(entry[1]),
  );
  return given.length ? Object.fromEntries(given) : null;
}

export function agentFromForm(values: AgentNodeForm) {
  return {
    agent_id: values.agent_id || null,
    agent_version_id: values.agent_version_id || null,
    instruction: values.instruction,
    inputs: nameVarFromForm(values.inputs),
    files_var: values.files_var.trim() || null,
    result: {
      format: values.result_format,
      schema: values.result_format === "json" ? parseJsonText(values.result_schema_text) : null,
      files: values.result_files,
    },
    limits: agentLimits(values),
    approval_mode: values.approval_mode,
    session_mode: values.session_mode,
  };
}

// loop (설계 2.4, 서버 LoopConfig). 반복 변수의 처음 값은 상류 변수나 고정값이다.

export function loopSchema(t: T, tw: TW) {
  return z.object({
    loop_vars: z
      .array(
        z
          .object({
            name: nameSchema(t, tw).refine((value) => value !== LOOP_COUNT_OUTPUT, {
              error: tw("forms.loop.reserved"),
            }),
            type: z.enum(VAR_TYPE_OPTIONS),
            source: z.enum(["value", "var"]),
            var: z.string(),
            value_text: z.string(),
          })
          .superRefine((item, ctx) => {
            if (item.source === "var") {
              const text = item.var.trim();
              if (!text || !parseSelector(text)) {
                ctx.addIssue({ code: "custom", path: ["var"], message: tw("validation.selector") });
              }
            } else if (item.type === "number" && item.value_text.trim()) {
              if (!isNumeric(item.value_text)) {
                ctx.addIssue({
                  code: "custom",
                  path: ["value_text"],
                  message: tw("validation.number"),
                });
              }
            }
          }),
      )
      .superRefine(uniqueBy("name", t("validation.duplicate"))),
    break_conditions: z.array(conditionSchema(t, tw)),
    logical: z.enum(["and", "or"]),
    max_iterations: integer(t, 1, LOOP_MAX_ITERATIONS),
  });
}
export type LoopForm = z.infer<ReturnType<typeof loopSchema>>;
export type LoopVarForm = LoopForm["loop_vars"][number];

export function loopToForm(config: Record<string, unknown>): LoopForm {
  return {
    loop_vars: records(config.loop_vars).map((item) => ({
      name: str(item.name),
      type: oneOf(VAR_TYPE_OPTIONS, item.type, "any"),
      source: typeof item.var === "string" ? "var" : "value",
      var: str(item.var),
      value_text: literalText(item.value),
    })),
    break_conditions: records(config.break_conditions).map(conditionToForm),
    logical: config.logical === "or" ? "or" : "and",
    max_iterations: num(config.max_iterations, LOOP_DEFAULT_ITERATIONS),
  };
}

// 빈 칸은 글 타입이면 빈 글, 아니면 null(처음 값 없음)이다. 서버는 var와 value 키가 함께 있으면 거부한다.
export function loopFromForm(values: LoopForm) {
  return {
    loop_vars: values.loop_vars.map((item) => {
      const base = { name: item.name.trim(), type: item.type };
      if (item.source === "var") return { ...base, var: item.var.trim() };
      const empty = item.value_text.trim() === "" && item.type !== "string";
      return { ...base, value: empty ? null : literalValue(item.value_text, item.type) };
    }),
    break_conditions: values.break_conditions.map(conditionFromForm),
    logical: values.logical,
    max_iterations: values.max_iterations,
  };
}

export function newLoopVar(): LoopVarForm {
  return { name: "", type: "string", source: "value", var: "", value_text: "" };
}

// list_operator (설계 2.4, 서버 ListOperatorConfig). 거르기·정렬·자르기·꺼내기는 켠 것만 보낸다.

export const LIST_SORT_ORDERS = ["asc", "desc"] as const;
// 파일 배열 원소의 key(서버 list_operator.FILE_KEYS)와 그 값의 타입
export const FILE_ITEM_KEYS = {
  name: "string",
  size: "number",
  mime: "string",
  extension: "string",
} as const satisfies Record<string, VarType>;

export function listOperatorSchema(t: T, tw: TW) {
  const condition = z
    .object({ key: z.string(), op: z.enum(IF_ELSE_OPS), value_text: z.string() })
    .superRefine((item, ctx) => checkConditionValue(t, tw, item, ctx));
  return z
    .object({
      input_var: selectorSchema(tw),
      filter_enabled: z.boolean(),
      filter: z.object({ logical: z.enum(["and", "or"]), conditions: z.array(condition) }),
      sort_enabled: z.boolean(),
      sort: z.object({ key: z.string(), order: z.enum(LIST_SORT_ORDERS) }),
      slice_enabled: z.boolean(),
      slice: z.object({ offset: integer(t, 0), limit: integer(t, 0).nullable() }),
      extract_enabled: z.boolean(),
      // 음수면 뒤에서 센다(-1이 마지막).
      extract_index: z
        .number({ error: t("validation.number") })
        .int({ error: t("validation.integer") }),
    })
    .superRefine((values, ctx) => {
      if (values.filter_enabled && values.filter.conditions.length === 0) {
        ctx.addIssue({
          code: "custom",
          path: ["filter", "conditions"],
          message: tw("validation.list"),
        });
      }
    });
}
export type ListOperatorForm = z.infer<ReturnType<typeof listOperatorSchema>>;
export type ItemConditionForm = ListOperatorForm["filter"]["conditions"][number];

export function listOperatorToForm(config: Record<string, unknown>): ListOperatorForm {
  const filter = record(config.filter);
  const sort = record(config.sort);
  const slice = record(config.slice);
  return {
    input_var: str(config.input_var),
    filter_enabled: config.filter !== null && config.filter !== undefined,
    filter: {
      logical: filter.logical === "or" ? "or" : "and",
      conditions: records(filter.conditions).map((item) => ({
        key: str(item.key),
        op: oneOf(IF_ELSE_OPS, item.op, "="),
        value_text: conditionValueText(item.value),
      })),
    },
    sort_enabled: config.sort !== null && config.sort !== undefined,
    sort: { key: str(sort.key), order: sort.order === "desc" ? "desc" : "asc" },
    slice_enabled: config.slice !== null && config.slice !== undefined,
    slice: { offset: num(slice.offset, 0), limit: numOrNull(slice.limit) },
    extract_enabled: typeof config.extract_index === "number",
    extract_index: num(config.extract_index, 0),
  };
}

export function listOperatorFromForm(values: ListOperatorForm) {
  const { offset, limit } = values.slice;
  return {
    input_var: values.input_var.trim(),
    filter: values.filter_enabled
      ? {
          logical: values.filter.logical,
          conditions: values.filter.conditions.map((item) => ({
            key: item.key.trim(),
            op: item.op,
            value: conditionValue(item.op, item.value_text),
          })),
        }
      : null,
    sort: values.sort_enabled ? { key: values.sort.key.trim(), order: values.sort.order } : null,
    slice: values.slice_enabled
      ? {
          offset: Number.isFinite(offset) ? offset : 0,
          limit: limit !== null && Number.isFinite(limit) ? limit : null,
        }
      : null,
    extract_index:
      values.extract_enabled && Number.isFinite(values.extract_index) ? values.extract_index : null,
  };
}

export function newItemCondition(): ItemConditionForm {
  return { key: "", op: "=", value_text: "" };
}

// document_extractor (설계 2.4, 서버 DocumentExtractorConfig)

export function documentExtractorSchema(tw: TW) {
  return z.object({ file_var: selectorSchema(tw) });
}
export type DocumentExtractorForm = z.infer<ReturnType<typeof documentExtractorSchema>>;
export const documentExtractorToForm = (
  config: Record<string, unknown>,
): DocumentExtractorForm => ({ file_var: str(config.file_var) });
export const documentExtractorFromForm = (values: DocumentExtractorForm) => ({
  file_var: values.file_var.trim(),
});

// tool (설계 2.4, 서버 ToolConfig). 매개변수마다 값을 템플릿 글·변수·고정값 중 하나로 준다.

export const TOOL_ARG_MODES = ["template", "var", "const"] as const;
export type ToolArgMode = (typeof TOOL_ARG_MODES)[number];

export interface ToolForm {
  tool: string;
  params: Record<string, { mode: ToolArgMode; value_text: string }>;
}

interface ToolParamLike {
  name: string;
  type: string;
  required: boolean;
  default?: unknown;
  max_length?: number | null;
}

interface ToolLike {
  name: string;
  params: readonly ToolParamLike[];
}

// 비우면 서버가 기본값을 쓴다. 기본값도 없는 필수 매개변수만 채워야 한다.
export function paramNeedsValue(param: ToolParamLike): boolean {
  return param.required && (param.default === null || param.default === undefined);
}

export function toolSchema(t: T, tw: TW, tools: readonly ToolLike[]) {
  const arg = z.object({ mode: z.enum(TOOL_ARG_MODES), value_text: z.string() });
  return z
    .object({
      tool: z.string().min(1, { error: tw("forms.tool.pick") }),
      params: z.record(z.string(), arg),
    })
    .superRefine((values, ctx) => {
      const tool = tools.find((item) => item.name === values.tool);
      if (!tool) return;
      for (const param of tool.params) {
        const value = values.params[param.name];
        const text = value?.value_text ?? "";
        const path = ["params", param.name, "value_text"];
        if (!text.trim()) {
          if (paramNeedsValue(param)) {
            ctx.addIssue({ code: "custom", path, message: t("validation.required") });
          }
          continue;
        }
        if (value?.mode === "var" && !parseSelector(text)) {
          ctx.addIssue({ code: "custom", path, message: tw("validation.selectorFormat") });
        } else if (value?.mode === "const" && param.type === "number" && !isNumeric(text)) {
          ctx.addIssue({ code: "custom", path, message: tw("validation.number") });
        } else if (
          value?.mode !== "var" &&
          typeof param.max_length === "number" &&
          text.length > param.max_length
        ) {
          ctx.addIssue({
            code: "custom",
            path,
            message: t("validation.maxLength", { max: param.max_length }),
          });
        }
      }
    });
}

export function toolToForm(config: Record<string, unknown>): ToolForm {
  const params: ToolForm["params"] = {};
  for (const [name, value] of Object.entries(record(config.params))) {
    const arg = record(value);
    params[name] = {
      mode: oneOf(TOOL_ARG_MODES, arg.mode, "template"),
      value_text: literalText(arg.value),
    };
  }
  return { tool: str(config.tool), params };
}

// 고정값은 매개변수 타입(string·number·boolean)으로 맞춰 넣는다. 고른 도구에 없는 매개변수는 버린다.
export function toolFromForm(values: ToolForm, tools: readonly ToolLike[]) {
  const tool = tools.find((item) => item.name === values.tool);
  const params: Record<string, { mode: ToolArgMode; value: unknown }> = {};
  for (const param of tool?.params ?? []) {
    const arg = values.params[param.name] ?? { mode: "template", value_text: "" };
    const text = arg.mode === "var" ? arg.value_text.trim() : arg.value_text;
    let value: unknown = text;
    if (arg.mode === "const" && text.trim() !== "" && param.type !== "string") {
      value = literalValue(text, param.type as VarType);
    }
    params[param.name] = { mode: arg.mode, value };
  }
  return { tool: values.tool, params };
}

// 도구를 바꾸면 그 도구의 매개변수로 칸을 다시 만든다. 비운 칸은 서버가 기본값을 쓴다.
export function toolParamsForm(tool: ToolLike | undefined): ToolForm["params"] {
  const params: ToolForm["params"] = {};
  for (const param of tool?.params ?? []) params[param.name] = { mode: "template", value_text: "" };
  return params;
}

// human_input (설계 2.4, 서버 HumanInputConfig). 필드는 start 필드와 같은 모양이고 하나 이상이어야 한다.

export function humanInputSchema(t: T, tw: TW) {
  return z.object({ description: z.string(), fields: startFieldsSchema(t, tw, 1) });
}
export type HumanInputForm = z.infer<ReturnType<typeof humanInputSchema>>;
export const humanInputToForm = (config: Record<string, unknown>): HumanInputForm => ({
  description: str(config.description),
  fields: startToForm(config).fields,
});
export const humanInputFromForm = (values: HumanInputForm) => ({
  description: values.description,
  fields: startFromForm({ fields: values.fields }).fields,
});
