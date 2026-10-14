import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import * as z from "zod";
import { isApiError } from "@/api/http";
import type { WorkflowOut, WorkflowSettings } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { RadioCards } from "@/components/ui/radio-cards";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { usePatchWorkflow } from "@/features/workflows/api";
import { useFlowActions, useFlowStoreApi } from "@/features/workflows/flowStoreContext";
import { errorMessage } from "@/lib/errors";
import {
  integer,
  requiredText,
  SLUG_PATTERN,
  toNumber,
  toNumberOrNull,
  type T,
} from "@/lib/formSchemas";
import { applyServerErrors } from "@/lib/serverErrors";

function basicsSchema(t: T) {
  return z.object({
    name: requiredText(t, 100),
    slug: z
      .string()
      .trim()
      .regex(SLUG_PATTERN, { error: t("validation.slug") }),
    icon: z
      .string()
      .trim()
      .max(64, { error: t("validation.maxLength", { max: 64 }) }),
    description: z
      .string()
      .trim()
      .max(2000, { error: t("validation.maxLength", { max: 2000 }) }),
  });
}

type BasicsForm = z.infer<ReturnType<typeof basicsSchema>>;

function BasicsPanel({ workflow }: { workflow: WorkflowOut }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const patch = usePatchWorkflow(workflow.id);
  const schema = useMemo(() => basicsSchema(tc), [tc]);
  const form = useForm<BasicsForm>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: workflow.name,
      slug: workflow.slug,
      icon: workflow.icon ?? "",
      description: workflow.description ?? "",
    },
  });
  const { errors, isDirty, isSubmitting } = form.formState;
  const submit = form.handleSubmit(async (values) => {
    try {
      const saved = await patch.mutateAsync({
        name: values.name,
        slug: values.slug,
        icon: values.icon || null,
        description: values.description,
      });
      form.reset({
        name: saved.name,
        slug: saved.slug,
        icon: saved.icon ?? "",
        description: saved.description ?? "",
      });
      toast.success(t("settings.basicsSaved"));
    } catch (error) {
      if (isApiError(error, 409) && error.code === "slug_taken") {
        form.setError("slug", { type: "server", message: error.message });
        return;
      }
      const leftover = applyServerErrors(error, form.setError, (loc) => {
        const head = String(loc[0] ?? "");
        return ["name", "slug", "icon", "description"].includes(head) ? head : null;
      });
      toast.error(leftover.length ? leftover.join(" ") : errorMessage(error, tc));
    }
  });
  return (
    <Panel
      title={t("settings.basics")}
      description={t("settings.kindFixed", { kind: t(`kind.${workflow.kind}`) })}
    >
      <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
        <div className="grid grid-cols-[5rem_minmax(0,1fr)] gap-3">
          <Field label={t("create.icon")} error={errors.icon?.message}>
            <Input {...form.register("icon")} />
          </Field>
          <Field label={t("create.name")} error={errors.name?.message} required>
            <Input {...form.register("name")} />
          </Field>
        </div>
        <Field label={t("create.slug")} hint={t("create.slugHint")} error={errors.slug?.message}>
          <Input className="font-mono" {...form.register("slug")} />
        </Field>
        <Field label={t("create.description")} error={errors.description?.message}>
          <Textarea rows={2} {...form.register("description")} />
        </Field>
        <div>
          <Button type="submit" size="sm" disabled={!isDirty} loading={isSubmitting}>
            {t("settings.saveBasics")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}

function runSchema(t: T) {
  return z.object({
    max_run_seconds: integer(t, 60, 7200),
    max_total_tokens: integer(t, 1),
    max_cost_usd: z
      .number({ error: t("validation.number") })
      .gt(0, { error: t("validation.positive") })
      .nullable(),
    approval_mode: z.enum(["inherit", "off"]),
    mask_secrets_in_outputs: z.boolean(),
  });
}

type RunForm = z.infer<ReturnType<typeof runSchema>>;

function RunSettingsPanel({ initial }: { initial: WorkflowSettings }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const store = useFlowStoreApi();
  const actions = useFlowActions();
  const schema = useMemo(() => runSchema(tc), [tc]);
  const form = useForm<RunForm>({
    resolver: zodResolver(schema),
    mode: "onChange",
    defaultValues: {
      max_run_seconds: initial.limits?.max_run_seconds ?? 1800,
      max_total_tokens: initial.limits?.max_total_tokens ?? 2_000_000,
      max_cost_usd: initial.limits?.max_cost_usd ?? null,
      approval_mode: initial.approval_mode,
      mask_secrets_in_outputs: initial.mask_secrets_in_outputs,
    },
  });
  const [blocked, setBlocked] = useState(false);
  const errors = form.formState.errors;
  const approval = useWatch({ control: form.control, name: "approval_mode" });

  useEffect(() => {
    return form.subscribe({
      formState: { values: true },
      callback: (info) => {
        if (info.name === undefined) return;
        const parsed = schema.safeParse(form.getValues());
        setBlocked(!parsed.success);
        if (!parsed.success) return;
        const { max_run_seconds, max_total_tokens, max_cost_usd, ...rest } = parsed.data;
        actions.setSettings({
          ...store.getState().settings,
          ...rest,
          limits: { max_run_seconds, max_total_tokens, max_cost_usd },
        });
      },
    });
  }, [form, schema, actions, store]);

  return (
    <Panel title={t("settings.limits")}>
      <div className="flex flex-col gap-4">
        {blocked ? <Alert tone="warn" title={t("settings.invalid")} /> : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            label={t("settings.maxRunSeconds")}
            hint={t("settings.maxRunSecondsHint")}
            error={errors.max_run_seconds?.message}
          >
            <Input
              type="number"
              step="1"
              {...form.register("max_run_seconds", { setValueAs: toNumber })}
            />
          </Field>
          <Field
            label={t("settings.maxTotalTokens")}
            hint={t("settings.maxTotalTokensHint")}
            error={errors.max_total_tokens?.message}
          >
            <Input
              type="number"
              step="1"
              {...form.register("max_total_tokens", { setValueAs: toNumber })}
            />
          </Field>
          <Field
            label={t("settings.maxCostUsd")}
            hint={t("settings.maxCostUsdHint")}
            error={errors.max_cost_usd?.message}
          >
            <Input
              type="number"
              step="any"
              {...form.register("max_cost_usd", { setValueAs: toNumberOrNull })}
            />
          </Field>
        </div>
        <Field label={t("settings.approval")}>
          <Controller
            control={form.control}
            name="approval_mode"
            render={({ field }) => (
              <RadioCards
                ariaLabel={t("settings.approval")}
                value={field.value}
                onValueChange={field.onChange}
                options={[
                  {
                    value: "inherit",
                    label: t("settings.approvals.inherit"),
                    description: t("settings.approvals.inheritHint"),
                  },
                  {
                    value: "off",
                    label: t("settings.approvals.off"),
                    description: t("settings.approvals.offHint"),
                  },
                ]}
              />
            )}
          />
        </Field>
        {approval === "off" ? <Alert tone="warn" title={t("settings.offWarning")} /> : null}
        <Field inline label={t("settings.mask")} hint={t("settings.maskHint")}>
          <Controller
            control={form.control}
            name="mask_secrets_in_outputs"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
      </div>
    </Panel>
  );
}

// 오른쪽 패널의 설정 탭: 기본 정보(PATCH, 따로 저장), 실행 한도·승인·마스킹(초안 settings, 자동 저장).
export function WorkflowSettingsPanel({
  workflow,
  settings,
}: {
  workflow: WorkflowOut;
  settings: WorkflowSettings;
}) {
  return (
    <div className="flex flex-col gap-4">
      <BasicsPanel workflow={workflow} />
      <RunSettingsPanel initial={settings} />
    </div>
  );
}
