import * as z from "zod";
import type { WorkspaceOut, WorkspacePatch } from "@/api/types";
import {
  approvalModeSchema,
  decimal,
  DEFAULT_RUN_LIMITS,
  integer,
  requiredText,
  runLimitsSchema,
  type T,
} from "@/lib/formSchemas";

export function makeWorkspaceSettingsSchema(t: T) {
  return z.object({
    name: requiredText(t, 100),
    approval_mode_default: approvalModeSchema,
    default_limits: runLimitsSchema(t),
    run_event_retention_days: integer(t, 1, 3650),
    // 화면에서는 모델별 가격을 행 목록으로 다루고 저장할 때 사전으로 바꾼다.
    model_prices: z
      .array(
        z.object({
          model: requiredText(t, 200),
          input_per_mtok: decimal(t, 0),
          output_per_mtok: decimal(t, 0),
          cached_input_per_mtok: decimal(t, 0).nullable(),
        }),
      )
      .superRefine((rows, ctx) => {
        const seen = new Set<string>();
        rows.forEach((row, index) => {
          const key = row.model.trim();
          if (key && seen.has(key)) {
            ctx.addIssue({
              code: "custom",
              path: [index, "model"],
              message: t("validation.duplicate"),
            });
          }
          seen.add(key);
        });
      }),
  });
}

export type WorkspaceSettingsForm = z.infer<ReturnType<typeof makeWorkspaceSettingsSchema>>;

export function workspaceToForm(workspace: WorkspaceOut): WorkspaceSettingsForm {
  const settings = workspace.settings;
  const limits = settings.default_limits ?? DEFAULT_RUN_LIMITS;
  return {
    name: workspace.name,
    approval_mode_default: settings.approval_mode_default,
    default_limits: {
      max_steps: limits.max_steps,
      max_run_seconds: limits.max_run_seconds,
      max_total_tokens: limits.max_total_tokens,
      max_cost_usd: limits.max_cost_usd ?? null,
      context_budget_tokens: limits.context_budget_tokens ?? null,
      tool_timeout_sec: limits.tool_timeout_sec,
    },
    run_event_retention_days: settings.run_event_retention_days,
    model_prices: Object.entries(settings.model_prices ?? {}).map(([model, price]) => ({
      model,
      input_per_mtok: price.input_per_mtok,
      output_per_mtok: price.output_per_mtok,
      cached_input_per_mtok: price.cached_input_per_mtok ?? null,
    })),
  };
}

export function formToWorkspacePatch(form: WorkspaceSettingsForm): WorkspacePatch {
  return {
    name: form.name,
    settings: {
      approval_mode_default: form.approval_mode_default,
      default_limits: form.default_limits,
      run_event_retention_days: form.run_event_retention_days,
      model_prices: Object.fromEntries(
        form.model_prices.map((row) => [
          row.model.trim(),
          {
            input_per_mtok: row.input_per_mtok,
            output_per_mtok: row.output_per_mtok,
            cached_input_per_mtok: row.cached_input_per_mtok,
          },
        ]),
      ),
    },
  };
}
