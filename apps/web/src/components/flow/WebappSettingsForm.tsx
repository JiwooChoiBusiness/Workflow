import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import * as z from "zod";
import type { WebappSettings } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { splitLines } from "@/features/workflows/configSchemas";
import { useFlowActions, useFlowStoreApi } from "@/features/workflows/flowStoreContext";
import type { T } from "@/lib/formSchemas";

// 서버 WebappSettings(apps/api/app/schemas/workflow.py)와 같은 상한
function webappSchema(t: T) {
  return z.object({
    title: z
      .string()
      .trim()
      .max(100, { error: t("validation.maxLength", { max: 100 }) }),
    description: z
      .string()
      .trim()
      .max(2000, { error: t("validation.maxLength", { max: 2000 }) }),
    opening_statement: z.string().max(4000, { error: t("validation.maxLength", { max: 4000 }) }),
    suggested_text: z.string().superRefine((value, ctx) => {
      const lines = splitLines(value);
      if (lines.length > 10) {
        ctx.addIssue({ code: "custom", message: t("validation.maxItems", { max: 10 }) });
      } else if (lines.some((line) => line.length > 200)) {
        ctx.addIssue({ code: "custom", message: t("validation.maxLength", { max: 200 }) });
      }
    }),
  });
}

type WebappForm = z.infer<ReturnType<typeof webappSchema>>;

function toForm(webapp: WebappSettings | undefined): WebappForm {
  return {
    title: webapp?.title ?? "",
    description: webapp?.description ?? "",
    opening_statement: webapp?.opening_statement ?? "",
    suggested_text: (webapp?.suggested_questions ?? []).join("\n"),
  };
}

function fromForm(values: WebappForm): WebappSettings {
  return {
    title: values.title.trim() || null,
    description: values.description.trim() || null,
    opening_statement: values.opening_statement || null,
    suggested_questions: splitLines(values.suggested_text),
  };
}

// 웹앱 설정(설계 7.3 WebappSettingsForm). settings.webapp에 들어가 발행 때 함께 고정된다.
export function WebappSettingsForm({ initial }: { initial: WebappSettings | undefined }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const store = useFlowStoreApi();
  const actions = useFlowActions();
  const schema = useMemo(() => webappSchema(tc), [tc]);
  const form = useForm<WebappForm>({
    defaultValues: toForm(initial),
    resolver: zodResolver(schema),
    mode: "onChange",
  });
  const [blocked, setBlocked] = useState(false);
  const errors = form.formState.errors;

  useEffect(() => {
    return form.subscribe({
      formState: { values: true },
      callback: (info) => {
        if (info.name === undefined) return;
        const parsed = schema.safeParse(form.getValues());
        setBlocked(!parsed.success);
        if (parsed.success) {
          actions.setSettings({ ...store.getState().settings, webapp: fromForm(parsed.data) });
        }
      },
    });
  }, [form, schema, actions, store]);

  return (
    <Panel title={t("webapp.title")} description={t("webapp.description")}>
      <div className="flex flex-col gap-3">
        {blocked ? <Alert tone="warn" title={t("settings.invalid")} /> : null}
        <Field
          label={t("webapp.appTitle")}
          hint={t("webapp.appTitleHint")}
          error={errors.title?.message}
        >
          <Input {...form.register("title")} />
        </Field>
        <Field label={t("webapp.appDescription")} error={errors.description?.message}>
          <Textarea rows={2} {...form.register("description")} />
        </Field>
        <Field
          label={t("webapp.opening")}
          hint={t("webapp.openingHint")}
          error={errors.opening_statement?.message}
        >
          <Textarea rows={3} {...form.register("opening_statement")} />
        </Field>
        <Field
          label={t("webapp.suggested")}
          hint={t("webapp.suggestedHint")}
          error={errors.suggested_text?.message}
        >
          <Textarea rows={4} {...form.register("suggested_text")} />
        </Field>
      </div>
    </Panel>
  );
}
