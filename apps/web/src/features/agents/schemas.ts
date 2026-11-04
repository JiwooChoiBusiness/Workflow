import * as z from "zod";
import {
  TOOL_GROUPS,
  type AgentOut,
  type AgentPatch,
  type AgentVersionOut,
  type DraftUpdate,
  type ReasoningEffort,
  type ToolGroup,
} from "@/api/types";
import {
  approvalModeSchema,
  decimal,
  DEFAULT_RUN_LIMITS,
  ENV_NAME_PATTERN,
  integer,
  requiredText,
  runLimitsSchema,
  SLUG_PATTERN,
  type T,
} from "@/lib/formSchemas";
import {
  DEFAULT_RETRIEVAL,
  fromRetrievalForm,
  retrievalErrors,
  toRetrievalForm,
  type RetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { formToSubAgents, subAgentsSchema, subAgentsToForm } from "./subAgents";

// 능력표를 모를 때 쓰는 출력 상한(설계 4.3의 보수적 기본값)
export const DEFAULT_MAX_OUTPUT_TOKENS = 65_536;
// 서버 app/schemas/agent_version.py와 같은 값
const MAX_INSTRUCTIONS_CHARS = 100_000;
export const MAX_AGENT_KBS = 5;
// 서버 MAX_AGENT_SQL_SOURCES. 목록이 도구 설명과 시스템 프롬프트에 들어간다(3단계 10.3).
export const MAX_AGENT_SQL_SOURCES = 10;
// 지식 탭이 보이는 저장 경고(3단계 8.4, 10.3). 하위 에이전트 때문인 paid_key_required는 agent_id가, 데이터
// 소스 때문이면 source_id가 있다.
export const KNOWLEDGE_WARNING_CODES: ReadonlySet<string> = new Set([
  "knowledge_base_not_found",
  "paid_key_required",
  "retrieval_settings_required",
  "sql_source_not_found",
]);
// KB_TOP_K_MAX 기본값. 화면은 지식 기본값(GET /knowledge-defaults)의 top_k_max를 넘긴다.
export const DEFAULT_TOP_K_MAX = 20;
const MAX_ENV_VALUE_CHARS = 32 * 1024;

const toolGroup = z.enum(["shell", "files", "http", "ask_user"]) satisfies z.ZodType<ToolGroup>;
const reasoningEffort = z.enum([
  "minimal",
  "low",
  "medium",
  "high",
]) satisfies z.ZodType<ReasoningEffort>;

function optionalText(t: T, max: number) {
  return z
    .string()
    .trim()
    .max(max, { error: t("validation.maxLength", { max }) });
}

function optionalSlug(t: T) {
  return z
    .string()
    .trim()
    .refine((value) => value === "" || SLUG_PATTERN.test(value), { error: t("validation.slug") });
}

export function makeAgentCreateSchema(t: T) {
  return z.object({
    name: requiredText(t, 100),
    icon: optionalText(t, 64),
    slug: optionalSlug(t),
    description: optionalText(t, 2000),
  });
}

// 에이전트의 지식 베이스(3단계 8.4). 검색 설정을 끄면(이어받기) 서버에 null을 보낸다.
function knowledgeSchema(t: T, topKMax: number) {
  return z
    .object({
      kb_ids: z
        .array(z.string())
        .max(MAX_AGENT_KBS, { error: t("validation.maxItems", { max: MAX_AGENT_KBS }) }),
      custom_retrieval: z.boolean(),
      retrieval: z.custom<RetrievalForm>(),
    })
    .superRefine((value, ctx) => {
      if (!value.custom_retrieval) return;
      // 칸별 문구는 검색 설정 폼이 보인다. 여기서는 저장만 막는다.
      for (const field of Object.keys(retrievalErrors(value.retrieval, topKMax))) {
        ctx.addIssue({ code: "custom", path: ["retrieval", field], message: field });
      }
    });
}

export function makeAgentFormSchema(t: T, options: { maxOutputTokens: number; topKMax?: number }) {
  return z.object({
    meta: z.object({
      name: requiredText(t, 100),
      slug: z
        .string()
        .trim()
        .regex(SLUG_PATTERN, { error: t("validation.slug") }),
      icon: optionalText(t, 64),
      description: optionalText(t, 2000),
    }),
    instructions: z.string().max(MAX_INSTRUCTIONS_CHARS, {
      error: t("validation.maxLength", { max: MAX_INSTRUCTIONS_CHARS }),
    }),
    model: z.object({
      model_id: z.string().nullable(),
      params: z.object({
        temperature: decimal(t, 0, 2).nullable(),
        // 서버는 0을 받지 않는다(0 < top_p ≤ 1).
        top_p: z
          .number({ error: t("validation.number") })
          .gt(0, { error: t("validation.positive") })
          .max(1, { error: t("validation.max", { max: 1 }) })
          .nullable(),
        max_output_tokens: integer(t, 1, options.maxOutputTokens),
        reasoning_effort: reasoningEffort.nullable(),
      }),
    }),
    // 폴백·라이브 목록에 없는 모델도 저장할지. 저장되지 않는 요청 옵션이다.
    allow_unlisted_model: z.boolean(),
    tools: z.array(toolGroup),
    approval_mode: approvalModeSchema,
    limits: runLimitsSchema(t),
    sandbox: z.object({
      memory_limit_mb: integer(t, 1).nullable(),
      max_processes: integer(t, 1).nullable(),
    }),
    env_vars: z
      .array(
        z.object({
          name: z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .max(128, { error: t("validation.maxLength", { max: 128 }) })
            .regex(ENV_NAME_PATTERN, { error: t("validation.envName") }),
          value: z.string().max(MAX_ENV_VALUE_CHARS, {
            error: t("validation.maxLength", { max: MAX_ENV_VALUE_CHARS }),
          }),
          secret: z.boolean(),
          // 서버에 값이 있지만 응답에 오지 않은 변수(비밀 값 등). 값 칸을 비워 두면 그 값을 그대로 둔다.
          keep: z.boolean(),
        }),
      )
      .superRefine((vars, ctx) => {
        // Windows 환경변수 이름은 대소문자를 가리지 않는다(서버도 같은 규칙).
        const seen = new Set<string>();
        vars.forEach((item, index) => {
          const key = item.name.trim().toUpperCase();
          if (key && seen.has(key)) {
            ctx.addIssue({
              code: "custom",
              path: [index, "name"],
              message: t("validation.duplicate"),
            });
          }
          seen.add(key);
        });
      }),
    skill_version_ids: z.array(z.string()),
    file_ids: z.array(z.string()),
    sub_agents: subAgentsSchema(t),
    knowledge: knowledgeSchema(t, options.topKMax ?? DEFAULT_TOP_K_MAX),
    sql_sources: z.array(z.string()).max(MAX_AGENT_SQL_SOURCES, {
      error: t("validation.maxItems", { max: MAX_AGENT_SQL_SOURCES }),
    }),
  });
}

export type AgentCreateForm = z.infer<ReturnType<typeof makeAgentCreateSchema>>;
export type AgentForm = z.infer<ReturnType<typeof makeAgentFormSchema>>;

export function agentToForm(agent: AgentOut, draft: AgentVersionOut): AgentForm {
  const params = draft.model?.params;
  const limits = draft.limits ?? DEFAULT_RUN_LIMITS;
  return {
    meta: {
      name: agent.name,
      slug: agent.slug,
      icon: agent.icon ?? "",
      description: agent.description ?? "",
    },
    instructions: draft.instructions ?? "",
    model: {
      model_id: draft.model?.model_id ?? null,
      params: {
        temperature: params?.temperature ?? null,
        top_p: params?.top_p ?? null,
        max_output_tokens: params?.max_output_tokens ?? 16000,
        reasoning_effort: params?.reasoning_effort ?? null,
      },
    },
    allow_unlisted_model: false,
    tools: draft.tools ?? [...TOOL_GROUPS],
    approval_mode: draft.approval_mode,
    limits: {
      max_steps: limits.max_steps,
      max_run_seconds: limits.max_run_seconds,
      max_total_tokens: limits.max_total_tokens,
      max_cost_usd: limits.max_cost_usd ?? null,
      context_budget_tokens: limits.context_budget_tokens ?? null,
      tool_timeout_sec: limits.tool_timeout_sec,
      max_child_calls: limits.max_child_calls,
    },
    sandbox: {
      memory_limit_mb: draft.sandbox?.memory_limit_mb ?? null,
      max_processes: draft.sandbox?.max_processes ?? null,
    },
    env_vars: draft.env_vars.map((item) => ({
      name: item.name,
      value: item.value ?? "",
      secret: item.secret,
      keep: item.value === null,
    })),
    skill_version_ids: draft.skill_version_ids ?? [],
    file_ids: draft.file_ids ?? [],
    sub_agents: subAgentsToForm(draft.sub_agents),
    knowledge: {
      kb_ids: draft.knowledge?.kb_ids ?? [],
      custom_retrieval: Boolean(draft.knowledge?.retrieval_model),
      retrieval: toRetrievalForm(draft.knowledge?.retrieval_model ?? DEFAULT_RETRIEVAL),
    },
    sql_sources: draft.sql_sources ?? [],
  };
}

export function formToDraftUpdate(form: AgentForm, updatedAt: string): DraftUpdate {
  return {
    instructions: form.instructions,
    model: form.model,
    tools: form.tools,
    approval_mode: form.approval_mode,
    limits: form.limits,
    sandbox: form.sandbox,
    env_vars: form.env_vars.map((item) => ({
      name: item.name.trim(),
      secret: item.secret,
      // 서버는 value가 null이면 같은 이름으로 저장된 값을 그대로 둔다.
      value: item.keep && item.value === "" ? null : item.value,
    })),
    skill_version_ids: form.skill_version_ids,
    file_ids: form.file_ids,
    // 키가 빠지면 서버가 저장된 목록을 그대로 둔다. 모두 뺀 것도 저장되도록 늘 보낸다.
    sub_agents: formToSubAgents(form.sub_agents),
    knowledge: {
      kb_ids: form.knowledge.kb_ids,
      retrieval_model: form.knowledge.custom_retrieval
        ? fromRetrievalForm(form.knowledge.retrieval)
        : null,
    },
    // 지식 베이스처럼 늘 보낸다. 빠지면 서버가 저장된 목록을 그대로 둬서 모두 뺀 것이 저장되지 않는다.
    sql_sources: form.sql_sources,
    allow_unlisted_model: form.allow_unlisted_model,
    updated_at: updatedAt,
  };
}

export function formToAgentPatch(meta: AgentForm["meta"]): AgentPatch {
  return {
    name: meta.name,
    slug: meta.slug,
    // 빈 아이콘은 null로 보내 지운다.
    icon: meta.icon || null,
    description: meta.description,
  };
}

export function makeApiKeyCreateSchema(t: T) {
  return z.object({ name: requiredText(t, 100) });
}

export type ApiKeyCreateForm = z.infer<ReturnType<typeof makeApiKeyCreateSchema>>;
