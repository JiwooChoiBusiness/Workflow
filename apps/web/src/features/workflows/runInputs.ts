/*
 * 런 입력 폼(start 필드, 설계 2.3)의 값과 서버로 보낼 inputs. 편집기 실행 패널과 웹앱이 같이 쓴다. 최종
 * 검사·형 변환은 서버(start.coerce_inputs)가 하고 여기서는 빈 필수 칸과 숫자 모양만 먼저 본다.
 */
import type { FlowField, WfFileOut } from "@/api/types";
import { START_TYPE, type FlowGraph } from "./graph";

export type FieldType = FlowField["type"];

export interface UploadedFile {
  id: string;
  filename: string;
  size: number;
}

// 글·숫자·선택은 입력한 글자, 참·거짓은 boolean(정하지 않음은 null), 파일은 올린 파일 목록이다.
export type FieldValue = string | boolean | null | UploadedFile[];

const FIELD_TYPES: ReadonlySet<string> = new Set([
  "string",
  "paragraph",
  "number",
  "boolean",
  "select",
  "file",
  "file_list",
]);

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

// 초안 그래프의 start 필드를 웹앱 API(FlowField)와 같은 모양으로 읽는다.
export function startFieldsOf(graph: FlowGraph): FlowField[] {
  const start = graph.nodes.find((node) => node.type === START_TYPE && !node.parent_id);
  const raw = Array.isArray(start?.config.fields) ? start.config.fields : [];
  return raw.flatMap((item) => {
    const field = record(item);
    if (typeof field.name !== "string" || !field.name) return [];
    const type =
      typeof field.type === "string" && FIELD_TYPES.has(field.type) ? field.type : "string";
    return [
      {
        name: field.name,
        type: type as FieldType,
        label: typeof field.label === "string" ? field.label : "",
        required: field.required === true,
        default: field.default ?? null,
        options: Array.isArray(field.options) ? field.options.map(String) : null,
        max_length: typeof field.max_length === "number" ? field.max_length : null,
      },
    ];
  });
}

export function uploadedOf(file: WfFileOut): UploadedFile {
  return { id: file.id, filename: file.filename, size: file.size_bytes };
}

export function initialFieldValue(field: FlowField): FieldValue {
  const value: unknown = field.default;
  switch (field.type) {
    case "boolean":
      return typeof value === "boolean" ? value : null;
    case "file":
    case "file_list":
      return [];
    case "number":
      return typeof value === "number" ? String(value) : "";
    default:
      return typeof value === "string" ? value : "";
  }
}

export function initialValues(fields: readonly FlowField[]): Record<string, FieldValue> {
  return Object.fromEntries(fields.map((field) => [field.name, initialFieldValue(field)]));
}

export type InputProblem = "required" | "number";

export interface RunInputsResult {
  inputs: Record<string, unknown>;
  problems: Record<string, InputProblem>;
}

// 폼 값을 런 입력으로. 빈 선택 칸은 보내지 않아 서버가 기본값을 쓴다.
export function toRunInputs(
  fields: readonly FlowField[],
  values: Readonly<Record<string, FieldValue>>,
): RunInputsResult {
  const inputs: Record<string, unknown> = {};
  const problems: Record<string, InputProblem> = {};
  for (const field of fields) {
    const value = values[field.name] ?? initialFieldValue(field);
    let output: unknown = undefined;
    if (field.type === "file" || field.type === "file_list") {
      const files = Array.isArray(value) ? value : [];
      if (files.length > 0) {
        output = field.type === "file" ? files[0]!.id : files.map((file) => file.id);
      }
    } else if (field.type === "boolean") {
      if (typeof value === "boolean") output = value;
    } else {
      const text = typeof value === "string" ? value : "";
      if (field.type === "number") {
        const trimmed = text.trim();
        if (trimmed !== "") {
          const number = Number(trimmed);
          if (Number.isFinite(number)) output = number;
          else problems[field.name] = "number";
        }
      } else if (text !== "") {
        output = text;
      }
    }
    if (output === undefined) {
      if (field.required && !problems[field.name]) problems[field.name] = "required";
      continue;
    }
    inputs[field.name] = output;
  }
  return { inputs, problems };
}

// 서버 422 invalid_inputs의 details({필드: 메시지})를 칸 오류로.
export function serverInputErrors(details: unknown): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, value] of Object.entries(record(details))) {
    if (typeof value === "string") errors[key] = value;
  }
  return errors;
}
