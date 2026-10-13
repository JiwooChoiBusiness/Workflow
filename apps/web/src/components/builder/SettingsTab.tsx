import { Controller, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { ModelList } from "@/api/types";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import type { AgentForm } from "@/features/agents/schemas";
import { ModelPicker } from "./ModelPicker";

export function SettingsTab({
  form,
  models,
  modelsError,
  onRetryModels,
}: {
  form: UseFormReturn<AgentForm>;
  models: ModelList | undefined;
  modelsError: unknown;
  onRetryModels: () => void;
}) {
  const { t } = useTranslation("agents");
  const errors = form.formState.errors;
  return (
    <div className="flex flex-col gap-6">
      <Panel title={t("settings.basics")}>
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-3">
            <Field label={t("settings.icon")} error={errors.meta?.icon?.message}>
              <Input {...form.register("meta.icon")} />
            </Field>
            <Field label={t("settings.name")} error={errors.meta?.name?.message} required>
              <Input {...form.register("meta.name")} />
            </Field>
          </div>
          <Field
            label={t("settings.slug")}
            hint={t("settings.slugHint")}
            error={errors.meta?.slug?.message}
            required
          >
            <Input className="font-mono" {...form.register("meta.slug")} />
          </Field>
          <Field label={t("settings.description")} error={errors.meta?.description?.message}>
            <Textarea rows={2} {...form.register("meta.description")} />
          </Field>
        </div>
      </Panel>
      <Panel title={t("settings.instructions")} description={t("settings.instructionsHint")}>
        <Controller
          control={form.control}
          name="instructions"
          render={({ field }) => (
            <CodeEditor
              value={field.value}
              onChange={field.onChange}
              language="markdown"
              ariaLabel={t("settings.instructions")}
              minHeight={240}
              maxHeight={560}
            />
          )}
        />
      </Panel>
      <Panel title={t("settings.model")}>
        <ModelPicker
          form={form}
          models={models}
          modelsError={modelsError}
          onRetry={onRetryModels}
        />
      </Panel>
    </div>
  );
}
