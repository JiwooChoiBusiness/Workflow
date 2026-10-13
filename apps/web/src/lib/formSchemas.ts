import type { TFunction } from "i18next";
import * as z from "zod";
import { APPROVAL_MODES } from "@/api/types";

export type T = TFunction<"common">;

// 서버 SetupRequest·LoginRequest와 같은 규칙(app/schemas/auth.py)
export const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+$/;
export const SLUG_PATTERN = /^[a-z0-9-]{1,64}$/;
export const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

// <input type="number">의 문자열 값을 숫자로 바꾼다. 빈칸은 필수 칸이면 NaN(검증 실패), 선택 칸이면 null.
export function toNumber(value: unknown): number {
  if (value === "" || value === null || value === undefined) return Number.NaN;
  return Number(value);
}

export function toNumberOrNull(value: unknown): number | null {
  if (value === "" || value === null || value === undefined) return null;
  return Number(value);
}

export function requiredText(t: T, max: number) {
  return z
    .string()
    .trim()
    .min(1, { error: t("validation.required") })
    .max(max, { error: t("validation.maxLength", { max }) });
}

export function email(t: T) {
  return z
    .string()
    .trim()
    .min(1, { error: t("validation.required") })
    .max(254, { error: t("validation.maxLength", { max: 254 }) })
    .regex(EMAIL_PATTERN, { error: t("validation.email") });
}

export function integer(t: T, min: number, max?: number) {
  let schema = z
    .number({ error: t("validation.number") })
    .int({ error: t("validation.integer") })
    .min(min, { error: t("validation.min", { min }) });
  if (max !== undefined) schema = schema.max(max, { error: t("validation.max", { max }) });
  return schema;
}

export function decimal(t: T, min: number, max?: number) {
  let schema = z.number({ error: t("validation.number") }).min(min, {
    error: t("validation.min", { min }),
  });
  if (max !== undefined) schema = schema.max(max, { error: t("validation.max", { max }) });
  return schema;
}

export const approvalModeSchema = z.enum(["all_shell", "http_writes", "off"]) satisfies z.ZodType<
  (typeof APPROVAL_MODES)[number]
>;

// 서버 RunLimits 기본값(설계 4.6). 응답에 limits가 빠져 있을 때만 쓴다.
export const DEFAULT_RUN_LIMITS = {
  max_steps: 30,
  max_run_seconds: 1800,
  max_total_tokens: 2_000_000,
  max_cost_usd: null,
  context_budget_tokens: null,
  tool_timeout_sec: 120,
};

// 서버 RunLimits(app/schemas/common.py)와 같은 범위
export function runLimitsSchema(t: T) {
  return z.object({
    max_steps: integer(t, 1, 200),
    max_run_seconds: integer(t, 60, 7200),
    max_total_tokens: integer(t, 1),
    max_cost_usd: z
      .number({ error: t("validation.number") })
      .gt(0, { error: t("validation.positive") })
      .nullable(),
    context_budget_tokens: integer(t, 1).nullable(),
    tool_timeout_sec: integer(t, 1, 900),
  });
}
