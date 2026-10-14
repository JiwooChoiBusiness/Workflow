import * as z from "zod";
import type { SubAgentRef } from "@/api/types";
import type { T } from "@/lib/formSchemas";

// 서버 app/schemas/agent_version.py의 MAX_SUB_AGENTS와 같은 값
export const MAX_SUB_AGENTS = 10;

// JSON 결과의 스키마를 빌더에서 고정할지, 모델이 call_agent를 부를 때 output_schema로 정하게 둘지(오케스트레이션 2.2).
export type SchemaSource = "fixed" | "model";

export interface SubAgentForm {
  agent_id: string;
  format: "text" | "json";
  schema_source: SchemaSource;
  // 편집기의 글. 형식이 글이거나 모델이 정하는 항목은 저장하지 않지만, 형식을 다시 바꿀 때를 위해 들고 있다.
  schema_text: string;
  files: boolean;
  approval_mode: "inherit" | "off";
}

const STARTER_SCHEMA = {
  type: "object",
  properties: { summary: { type: "string" } },
  required: ["summary"],
  additionalProperties: false,
};

export const STARTER_SCHEMA_TEXT = JSON.stringify(STARTER_SCHEMA, null, 2);

export function newSubAgent(agentId: string): SubAgentForm {
  return {
    agent_id: agentId,
    format: "text",
    schema_source: "fixed",
    schema_text: STARTER_SCHEMA_TEXT,
    files: true,
    approval_mode: "inherit",
  };
}

export function subAgentsToForm(refs: readonly SubAgentRef[] | undefined): SubAgentForm[] {
  return (refs ?? []).map((ref) => {
    const result = ref.result;
    const schema = result?.schema ?? null;
    const json = result?.format === "json";
    return {
      agent_id: ref.agent_id,
      format: json ? "json" : "text",
      // 저장된 JSON 항목에 스키마가 없으면 모델이 정하는 항목이다.
      schema_source: json && schema === null ? "model" : "fixed",
      schema_text: schema ? JSON.stringify(schema, null, 2) : STARTER_SCHEMA_TEXT,
      files: result?.files ?? true,
      approval_mode: ref.approval_mode,
    };
  });
}

export function parseSchemaText(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    return typeof value === "object" && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export function formToSubAgents(items: readonly SubAgentForm[]): SubAgentRef[] {
  return items.map((item) => {
    const fixed = item.format === "json" && item.schema_source === "fixed";
    return {
      agent_id: item.agent_id,
      result: {
        format: item.format,
        // 서버는 형식이 글인 항목에 스키마가 있으면 받지 않는다.
        schema: fixed ? parseSchemaText(item.schema_text) : null,
        files: item.files,
      },
      approval_mode: item.approval_mode,
    };
  });
}

// 저장 전에 걸러 낼 수 있는 것만 본다. JSON Schema 자체의 문법은 서버(check_output_schema)가 본다.
export function schemaTextProblem(
  text: string,
): "jsonObject" | "schemaObject" | "schemaClosed" | null {
  const schema = parseSchemaText(text);
  if (!schema) return "jsonObject";
  if (schema.type !== "object") return "schemaObject";
  if (schema.additionalProperties !== false) return "schemaClosed";
  return null;
}

export function subAgentsSchema(t: T) {
  return z
    .array(
      z.object({
        agent_id: z.string(),
        format: z.enum(["text", "json"]),
        schema_source: z.enum(["fixed", "model"]),
        schema_text: z.string(),
        files: z.boolean(),
        approval_mode: z.enum(["inherit", "off"]),
      }),
    )
    .max(MAX_SUB_AGENTS, { error: t("validation.maxItems", { max: MAX_SUB_AGENTS }) })
    .superRefine((items, ctx) => {
      items.forEach((item, index) => {
        if (item.format !== "json" || item.schema_source !== "fixed") return;
        const problem = schemaTextProblem(item.schema_text);
        if (problem) {
          ctx.addIssue({
            code: "custom",
            path: [index, "schema_text"],
            message: t(`validation.${problem}`),
          });
        }
      });
    }) satisfies z.ZodType<SubAgentForm[]>;
}

// PUT draft 오류의 loc에서 "sub_agents" 뒤를 받아 폼 칸으로 옮긴다. 결과 스키마 오류는 편집기에, 그 밖의
// 항목 오류(없는 에이전트·자기 자신·순환·미발행)는 그 항목 줄에 붙인다.
export function subAgentErrorPath(rest: readonly (string | number)[]): string {
  const [index, field] = rest;
  if (index === undefined || Number.isNaN(Number(index))) return "sub_agents";
  if (field === "result") return `sub_agents.${index}.schema_text`;
  return `sub_agents.${index}.agent_id`;
}
