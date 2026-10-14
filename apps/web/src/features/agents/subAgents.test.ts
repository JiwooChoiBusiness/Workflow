import { describe, expect, it } from "vitest";
import type { SubAgentRef } from "@/api/types";
import {
  formToSubAgents,
  newSubAgent,
  parseSchemaText,
  schemaTextProblem,
  STARTER_SCHEMA_TEXT,
  subAgentErrorPath,
  subAgentsToForm,
} from "./subAgents";

const schema = {
  type: "object",
  properties: { title: { type: "string" } },
  required: ["title"],
  additionalProperties: false,
};

describe("하위 에이전트 폼 변환", () => {
  it("저장된 항목을 폼으로 옮기고 그대로 되돌린다", () => {
    const refs: SubAgentRef[] = [
      {
        agent_id: "a1",
        result: { format: "text", schema: null, files: true },
        approval_mode: "inherit",
      },
      { agent_id: "a2", result: { format: "json", schema, files: false }, approval_mode: "off" },
      {
        agent_id: "a3",
        result: { format: "json", schema: null, files: true },
        approval_mode: "inherit",
      },
    ];
    const form = subAgentsToForm(refs);
    expect(form.map((item) => [item.format, item.schema_source])).toEqual([
      ["text", "fixed"],
      ["json", "fixed"],
      // 스키마 없이 저장된 JSON 항목은 모델이 호출 때 정하는 항목이다.
      ["json", "model"],
    ]);
    expect(JSON.parse(form[1].schema_text)).toEqual(schema);
    expect(form[0].schema_text).toBe(STARTER_SCHEMA_TEXT);
    expect(formToSubAgents(form)).toEqual(refs);
  });

  it("result가 빠진 항목은 서버 기본값(글, 파일 복사)으로 본다", () => {
    expect(subAgentsToForm([{ agent_id: "a1", approval_mode: "inherit" }])).toEqual([
      newSubAgent("a1"),
    ]);
    expect(subAgentsToForm(undefined)).toEqual([]);
  });

  it("글 형식이나 모델이 정하는 항목은 편집기의 스키마를 보내지 않는다", () => {
    const text = { ...newSubAgent("a1"), schema_text: JSON.stringify(schema) };
    const model = { ...text, format: "json" as const, schema_source: "model" as const };
    expect(formToSubAgents([text, model]).map((ref) => ref.result?.schema)).toEqual([null, null]);
  });
});

describe("결과 스키마 검사", () => {
  it("JSON 객체가 아니면 받지 않는다", () => {
    expect(parseSchemaText("[1, 2]")).toBeNull();
    expect(parseSchemaText("{")).toBeNull();
    expect(schemaTextProblem("null")).toBe("jsonObject");
  });

  it("서버 규칙처럼 type은 object, additionalProperties는 false여야 한다", () => {
    expect(schemaTextProblem('{"type": "array"}')).toBe("schemaObject");
    expect(schemaTextProblem('{"type": "object"}')).toBe("schemaClosed");
    expect(schemaTextProblem(JSON.stringify(schema))).toBeNull();
  });
});

describe("저장 오류 위치", () => {
  it("결과 스키마 오류는 편집기에, 나머지는 그 항목 줄에 붙인다", () => {
    expect(subAgentErrorPath(["1", "result", "schema"])).toBe("sub_agents.1.schema_text");
    expect(subAgentErrorPath(["0", "agent_id"])).toBe("sub_agents.0.agent_id");
    expect(subAgentErrorPath([2])).toBe("sub_agents.2.agent_id");
    expect(subAgentErrorPath([])).toBe("sub_agents");
  });
});
