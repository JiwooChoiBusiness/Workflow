import { describe, expect, it } from "vitest";
import type { AgentOut, AgentVersionOut } from "@/api/types";
import i18n from "@/i18n";
import {
  agentToForm,
  formToAgentPatch,
  formToDraftUpdate,
  makeAgentCreateSchema,
  makeAgentFormSchema,
  type AgentForm,
} from "./schemas";

const t = i18n.getFixedT("ko", "common");

const agent: AgentOut = {
  id: "a1",
  slug: "sales-helper",
  name: "판매 정리",
  icon: null,
  description: "",
  webapp_enabled: false,
  draft_version_id: "v0",
  published_version_id: null,
  published_version_number: null,
  published_at: null,
  created_at: "2026-10-13T00:00:00Z",
  updated_at: "2026-10-13T00:00:00Z",
};

const draft: AgentVersionOut = {
  id: "v0",
  agent_id: "a1",
  version_number: null,
  status: "draft",
  instructions: "CSV를 정리한다.",
  model: { model_id: null, params: { max_output_tokens: 16000, reasoning_effort: "low" } },
  tools: ["shell", "files"],
  limits: {
    max_steps: 30,
    max_run_seconds: 1800,
    max_total_tokens: 2_000_000,
    max_cost_usd: null,
    context_budget_tokens: null,
    tool_timeout_sec: 120,
    max_child_calls: 20,
  },
  sandbox: { memory_limit_mb: null, max_processes: null },
  approval_mode: "all_shell",
  env_vars: [
    { name: "API_TOKEN", secret: true, value: null },
    { name: "REGION", secret: false, value: "kr" },
  ],
  skill_version_ids: ["s1"],
  file_ids: ["f1"],
  snapshot_id: null,
  published_at: null,
  change_note: null,
  created_at: "2026-10-13T00:00:00Z",
  updated_at: "2026-10-13T01:02:03.456789Z",
};

function issues(form: AgentForm, maxOutputTokens = 65_536) {
  const result = makeAgentFormSchema(t, { maxOutputTokens }).safeParse(form);
  return Object.fromEntries(
    (result.error?.issues ?? []).map((issue) => [issue.path.join("."), issue.message]),
  );
}

describe("에이전트 폼 스키마", () => {
  const form = agentToForm(agent, draft);

  it("서버 초안을 폼 값으로 옮기면 그대로 통과한다", () => {
    expect(issues(form)).toEqual({});
    expect(form.env_vars).toEqual([
      { name: "API_TOKEN", value: "", secret: true, keep: true },
      { name: "REGION", value: "kr", secret: false, keep: false },
    ]);
    expect(form.allow_unlisted_model).toBe(false);
  });

  it("한도와 모델 파라미터 범위를 서버와 같게 검사한다", () => {
    const bad: AgentForm = {
      ...form,
      model: {
        ...form.model,
        params: { temperature: 2.5, top_p: 0, max_output_tokens: 70_000, reasoning_effort: null },
      },
      limits: { ...form.limits, max_steps: 0, max_run_seconds: 30, tool_timeout_sec: 901 },
      sandbox: { memory_limit_mb: 0, max_processes: null },
    };
    expect(issues(bad)).toEqual({
      "model.params.temperature": "2 이하여야 합니다",
      "model.params.top_p": "0보다 커야 합니다",
      "model.params.max_output_tokens": "65536 이하여야 합니다",
      "limits.max_steps": "1 이상이어야 합니다",
      "limits.max_run_seconds": "60 이상이어야 합니다",
      "limits.tool_timeout_sec": "900 이하여야 합니다",
      "sandbox.memory_limit_mb": "1 이상이어야 합니다",
    });
  });

  it("출력 상한은 고른 모델의 능력표 값을 따른다", () => {
    const params = { ...form.model.params, max_output_tokens: 9000 };
    expect(issues({ ...form, model: { ...form.model, params } }, 8192)).toEqual({
      "model.params.max_output_tokens": "8192 이하여야 합니다",
    });
  });

  it("빈칸으로 둔 필수 숫자 칸(NaN)은 숫자 오류로 막는다", () => {
    expect(issues({ ...form, limits: { ...form.limits, max_steps: Number.NaN } })).toEqual({
      "limits.max_steps": "숫자를 입력하세요",
    });
  });

  it("환경변수 이름 규칙과 대소문자를 가리지 않는 중복을 검사한다", () => {
    const env_vars = [
      { name: "1BAD", value: "", secret: false, keep: false },
      { name: "token", value: "a", secret: false, keep: false },
      { name: "TOKEN", value: "b", secret: false, keep: false },
    ];
    expect(issues({ ...form, env_vars })).toEqual({
      "env_vars.0.name": "영문자·숫자·밑줄만 쓰고 숫자로 시작할 수 없습니다",
      "env_vars.2.name": "같은 이름이 이미 있습니다",
    });
  });

  it("하위 에이전트의 고정 JSON 스키마는 object이고 닫혀 있어야 한다", () => {
    const sub_agents: AgentForm["sub_agents"] = [
      {
        agent_id: "a2",
        format: "json",
        schema_source: "fixed",
        schema_text: '{"type": "object"}',
        files: true,
        approval_mode: "inherit",
      },
      {
        agent_id: "a3",
        format: "json",
        schema_source: "model",
        schema_text: "아무 글",
        files: false,
        approval_mode: "off",
      },
      {
        agent_id: "a4",
        format: "text",
        schema_source: "fixed",
        schema_text: "[]",
        files: true,
        approval_mode: "inherit",
      },
    ];
    expect(issues({ ...form, sub_agents })).toEqual({
      "sub_agents.0.schema_text": '스키마에 "additionalProperties": false가 있어야 합니다',
    });
    expect(issues({ ...form, limits: { ...form.limits, max_child_calls: 101 } })).toEqual({
      "limits.max_child_calls": "100 이하여야 합니다",
    });
  });

  it("슬러그와 이름을 검사한다", () => {
    expect(issues({ ...form, meta: { ...form.meta, name: " ", slug: "Bad Slug" } })).toEqual({
      "meta.name": "필수 항목입니다",
      "meta.slug": "영문 소문자, 숫자, 하이픈만 쓸 수 있습니다(64자까지)",
    });
  });
});

describe("초안 저장 본문", () => {
  it("값을 비워 둔 저장된 변수는 null로 보내 서버가 기존 값을 쓰게 한다", () => {
    const form = agentToForm(agent, draft);
    const body = formToDraftUpdate(
      {
        ...form,
        env_vars: [
          ...form.env_vars,
          { name: "NEW_SECRET", value: "s3cr3t", secret: true, keep: false },
        ],
      },
      draft.updated_at,
    );
    expect(body.env_vars).toEqual([
      { name: "API_TOKEN", secret: true, value: null },
      { name: "REGION", secret: false, value: "kr" },
      { name: "NEW_SECRET", secret: true, value: "s3cr3t" },
    ]);
    expect(body.updated_at).toBe(draft.updated_at);
    expect(body.allow_unlisted_model).toBe(false);
    expect(body.skill_version_ids).toEqual(["s1"]);
    // 하위 에이전트를 모두 뺀 저장도 서버에 닿도록 빈 목록이라도 키를 보낸다.
    expect(body.sub_agents).toEqual([]);
    expect(body.limits?.max_child_calls).toBe(20);
  });

  it("저장된 비밀 값을 새로 입력하면 그 값을 보낸다", () => {
    const form = agentToForm(agent, draft);
    form.env_vars[0] = { ...form.env_vars[0], value: "rotated" };
    expect(formToDraftUpdate(form, draft.updated_at).env_vars?.[0]).toEqual({
      name: "API_TOKEN",
      secret: true,
      value: "rotated",
    });
  });

  it("빈 아이콘은 null로 보내 지운다", () => {
    expect(formToAgentPatch({ name: "a", slug: "a", icon: "", description: "" }).icon).toBeNull();
  });
});

describe("에이전트 만들기 스키마", () => {
  const schema = makeAgentCreateSchema(t);

  it("슬러그는 비워도 되고 넣으면 규칙을 따른다", () => {
    expect(
      schema.safeParse({ name: "새 에이전트", icon: "", slug: "", description: "" }).success,
    ).toBe(true);
    expect(
      schema.safeParse({ name: "새 에이전트", icon: "", slug: "새", description: "" }).error
        ?.issues[0].path,
    ).toEqual(["slug"]);
  });
});
