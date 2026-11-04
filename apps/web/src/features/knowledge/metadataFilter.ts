/*
 * 메타데이터 필터(3단계 설계 7.5)의 폼 값과 서버 값 사이 변환, 검사. knowledge_retrieval 노드(값은 상수
 * 또는 변수)와 검색 시험(상수만)이 같이 쓴다. 연산자 이름은 Dify API 이름이 정식이고(GET /node-types의
 * kb_options.operators) 설계 7.5의 이름과 ASCII 기호는 서버처럼 별칭으로 읽는다.
 */
import type {
  MetadataCondition,
  MetadataFilterRequest,
  MetadataOperator,
  MetadataType,
} from "@/api/types";

export type FilterMode = "disabled" | "manual" | "automatic";
export const FILTER_MODES: readonly FilterMode[] = ["disabled", "manual", "automatic"];
export const MAX_FILTER_CONDITIONS = 20;

// 서버 app/schemas/retrieval.py OPERATOR_ALIASES
const OPERATOR_ALIASES: Record<string, string> = {
  "starts with": "start with",
  "ends with": "end with",
  "is empty": "empty",
  "is not empty": "not empty",
  "==": "=",
  "!=": "≠",
  ">=": "≥",
  "<=": "≤",
};

export const NO_VALUE_OPERATORS: ReadonlySet<string> = new Set(["empty", "not empty"]);
export const LIST_OPERATORS: ReadonlySet<string> = new Set(["in", "not in"]);

export function canonicalOperator(value: string): string {
  const key = value.trim().toLowerCase().split(/\s+/).join(" ");
  return OPERATOR_ALIASES[key] ?? key;
}

export interface FilterField {
  name: string;
  type: MetadataType;
}

export interface FilterConditionForm {
  name: string;
  // 조건을 만들 때 고른 필드의 타입. 저장된 config에는 타입이 없어 값 모양으로 짐작해 둔다. 필드 목록을
  // 읽은 뒤에는 목록의 타입을 쓴다(conditionType).
  type: MetadataType;
  comparison_operator: string;
  source: "const" | "var";
  value_text: string;
  var: string;
}

export interface MetadataFilterForm {
  mode: FilterMode;
  logical_operator: "and" | "or";
  conditions: FilterConditionForm[];
  model: { model_id: string };
}

export function emptyFilterForm(): MetadataFilterForm {
  return { mode: "disabled", logical_operator: "and", conditions: [], model: { model_id: "" } };
}

export function newCondition(field?: FilterField, operators?: Record<string, readonly string[]>) {
  const type = field?.type ?? "string";
  return {
    name: field?.name ?? "",
    type,
    comparison_operator: operators?.[type]?.[0] ?? "is",
    source: "const",
    value_text: "",
    var: "",
  } satisfies FilterConditionForm;
}

// 조건의 타입은 필드 목록이 정한다. 목록을 아직 못 읽었거나 목록에 없는 필드면 폼에 든 값을 임시로 쓴다.
export function conditionType(
  condition: Pick<FilterConditionForm, "name" | "type">,
  fields: readonly FilterField[] | null | undefined,
): MetadataType {
  const name = condition.name.trim();
  return fields?.find((field) => field.name === name)?.type ?? condition.type;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function isVarValue(value: unknown): value is { var: string } {
  return typeof record(value).var === "string";
}

function conditionToForm(raw: Record<string, unknown>): FilterConditionForm {
  const value = raw.value;
  const variable = isVarValue(value) ? value.var : null;
  const values = Array.isArray(value)
    ? value
    : value === null || value === undefined
      ? []
      : [value];
  const numeric = values.length > 0 && values.every((item) => typeof item === "number");
  return {
    name: typeof raw.name === "string" ? raw.name : "",
    type: numeric ? "number" : "string",
    comparison_operator: canonicalOperator(
      typeof raw.comparison_operator === "string" ? raw.comparison_operator : "is",
    ),
    source: variable !== null ? "var" : "const",
    value_text: variable !== null ? "" : values.map(String).join(", "),
    var: variable ?? "",
  };
}

export function toFilterForm(raw: unknown): MetadataFilterForm {
  const value = record(raw);
  const mode = FILTER_MODES.find((item) => item === value.mode) ?? "disabled";
  const conditions = Array.isArray(value.conditions)
    ? value.conditions.map((item) => conditionToForm(record(item)))
    : [];
  const modelId = record(value.model).model_id;
  return {
    mode,
    logical_operator: value.logical_operator === "or" ? "or" : "and",
    conditions,
    model: { model_id: typeof modelId === "string" ? modelId : "" },
  };
}

function isNumberText(text: string): boolean {
  return text.trim() !== "" && Number.isFinite(Number(text));
}

// 글 하나를 필드 타입의 값으로. number는 수, time은 Unix 초면 수이고 아니면 ISO 글이다.
function scalar(text: string, type: MetadataType): string | number {
  const trimmed = text.trim();
  if (type === "number" && isNumberText(trimmed)) return Number(trimmed);
  if (type === "time" && /^\d+$/.test(trimmed)) return Number(trimmed);
  return trimmed;
}

function listParts(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function constantValue(
  condition: FilterConditionForm,
  type: MetadataType,
): MetadataCondition["value"] {
  if (NO_VALUE_OPERATORS.has(condition.comparison_operator)) return null;
  if (LIST_OPERATORS.has(condition.comparison_operator)) {
    return listParts(condition.value_text).map((part) => scalar(part, type));
  }
  return scalar(condition.value_text, type);
}

/*
 * knowledge_retrieval 노드의 metadata_filter. 조건은 모드와 관계없이 보낸다. 서버는 manual일 때만 조건을
 * 검증하고 적용하므로(knowledge_retrieval.py) 끄거나 자동으로 바꿨다가 돌아와도 조건이 남는다. 변수를
 * 아직 고르지 않은 조건도 {var: ""}로 보내 다시 열 때 값 방식이 그대로다. 그 오류는 폼 스키마가 보인다.
 */
export function fromFilterForm(
  form: MetadataFilterForm,
  fields: readonly FilterField[] | null = null,
) {
  return {
    mode: form.mode,
    logical_operator: form.logical_operator,
    conditions: form.conditions.map((condition) => ({
      name: condition.name.trim(),
      comparison_operator: condition.comparison_operator,
      value: NO_VALUE_OPERATORS.has(condition.comparison_operator)
        ? null
        : condition.source === "var"
          ? { var: condition.var.trim() }
          : constantValue(condition, conditionType(condition, fields)),
    })),
    model: { model_id: form.model.model_id.trim() || null },
  };
}

// 검색 시험의 metadata_filter(상수만). 끄거나 조건이 없으면 보내지 않는다.
export function filterRequest(
  form: MetadataFilterForm,
  fields: readonly FilterField[] | null = null,
): MetadataFilterRequest | null {
  if (form.mode !== "manual" || form.conditions.length === 0) return null;
  return {
    mode: "manual",
    logical_operator: form.logical_operator,
    conditions: form.conditions.map((condition) => ({
      name: condition.name.trim(),
      // 고를 수 있는 연산자는 kb_options.operators에서 온 서버 이름뿐이다.
      comparison_operator: condition.comparison_operator as MetadataOperator,
      value: constantValue(condition, conditionType(condition, fields)),
    })),
    model: { model_id: null },
  };
}

/*
 * 여러 지식 베이스에서 같이 쓸 수 있는 필드: 모든 KB에 같은 이름·같은 타입으로 있는 것. 서버는 저장할 때만
 * metadata_field_mismatch·metadata_field_not_found로 알려 주므로 폼이 먼저 고를 수 있는 것만 보인다.
 */
export function commonFields(lists: readonly (readonly FilterField[])[]): FilterField[] {
  const [first, ...rest] = lists;
  if (!first) return [];
  return first
    .filter((field) =>
      rest.every((list) =>
        list.some((other) => other.name === field.name && other.type === field.type),
      ),
    )
    .map((field) => ({ name: field.name, type: field.type }));
}

export type ConditionProblem = "name" | "unknownField" | "operator" | "value" | "number" | "var";

/*
 * 수동 조건의 칸별 문제(조건 순번 → 칸 → 사유). fields가 null이면(아직 못 읽음) 필드 존재와 연산자는 보지
 * 않는다. 연산자는 필드 타입을 알아야 맞는지 알 수 있다. 서버의 metadata_condition_invalid와 같은 것을
 * 미리 막는다.
 */
export function conditionProblems(
  form: MetadataFilterForm,
  fields: readonly FilterField[] | null,
  operators: Record<string, readonly string[]>,
  isSelector: (text: string) => boolean = () => true,
): Record<number, Partial<Record<"name" | "comparison_operator" | "value", ConditionProblem>>> {
  const problems: Record<
    number,
    Partial<Record<"name" | "comparison_operator" | "value", ConditionProblem>>
  > = {};
  if (form.mode !== "manual") return problems;
  form.conditions.forEach((condition, index) => {
    const found: Partial<Record<"name" | "comparison_operator" | "value", ConditionProblem>> = {};
    const name = condition.name.trim();
    const field = fields?.find((item) => item.name === name);
    if (!name) found.name = "name";
    else if (fields && !field) found.name = "unknownField";
    const allowed = field ? operators[field.type] : undefined;
    if (allowed && !allowed.includes(condition.comparison_operator)) {
      found.comparison_operator = "operator";
    }
    if (!NO_VALUE_OPERATORS.has(condition.comparison_operator)) {
      if (condition.source === "var") {
        if (!condition.var.trim() || !isSelector(condition.var.trim())) found.value = "var";
      } else {
        const parts = LIST_OPERATORS.has(condition.comparison_operator)
          ? listParts(condition.value_text)
          : [condition.value_text.trim()].filter(Boolean);
        if (parts.length === 0) found.value = "value";
        else if (conditionType(condition, fields) === "number" && !parts.every(isNumberText)) {
          found.value = "number";
        }
      }
    }
    if (Object.keys(found).length > 0) problems[index] = found;
  });
  return problems;
}
