import { useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import {
  llmFromForm,
  llmSchema,
  llmToForm,
  PROMPT_ROLES,
  type LlmForm as LlmFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { toNumber } from "@/lib/formSchemas";
import { PromptEditor } from "../PromptEditor";
import { VariablePicker } from "../VariablePicker";
import { ModelSection } from "./ModelSection";
import {
  AddButton,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

export function LlmForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const kind = useFlow((state) => state.kind);
  const schema = useMemo(() => llmSchema(tc, t), [tc, t]);
  const form = useNodeForm<LlmFormValues>(node, schema, llmToForm, llmFromForm);
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "prompt" });
  const memoryEnabled = useWatch({ control: form.control, name: "memory.enabled" });
  const visionEnabled = useWatch({ control: form.control, name: "vision.enabled" });
  const jsonMode = useWatch({ control: form.control, name: "json_mode" });
  const roleOptions = PROMPT_ROLES.map((role) => ({
    value: role,
    label: t(`forms.llm.roles.${role}`),
  }));

  return (
    <div className="flex flex-col gap-4">
      <ModelSection form={form} />
      <Section
        title={t("forms.llm.prompt")}
        hint={t("forms.llm.systemHint")}
        actions={
          <AddButton
            label={t("forms.llm.addBlock")}
            onClick={() => append({ role: "user", text: "" })}
          />
        }
      >
        {fields.map((field, index) => {
          const block = t("forms.llm.block", { n: index + 1 });
          return (
            <div key={field.id} className="flex flex-col gap-1.5">
              <div className="flex items-center gap-1.5">
                <Controller
                  control={form.control}
                  name={`prompt.${index}.role`}
                  render={({ field: control }) => (
                    <Select
                      aria-label={`${block} ${t("forms.llm.role")}`}
                      className="h-7 w-32 text-xs"
                      value={control.value}
                      onValueChange={control.onChange}
                      options={roleOptions}
                    />
                  )}
                />
                <span className="flex-1" />
                <RemoveButton
                  label={t("forms.llm.removeBlock", { n: index + 1 })}
                  onClick={() => remove(index)}
                />
              </div>
              <Controller
                control={form.control}
                name={`prompt.${index}.text`}
                render={({ field: control }) => (
                  <PromptEditor
                    nodeId={node.id}
                    value={control.value}
                    onChange={control.onChange}
                    ariaLabel={block}
                    invalid={Boolean(serverIssue(`prompt.${index}.text`))}
                    minHeight={index === 0 ? 96 : 72}
                  />
                )}
              />
              <FieldMessage message={serverIssue(`prompt.${index}.text`)} />
            </div>
          );
        })}
        <FieldMessage message={errorAt(form, "prompt", serverIssue)} />
      </Section>
      <Section title={t("forms.llm.memory")}>
        {kind === "chatflow" ? (
          <>
            <Field inline label={t("forms.llm.memoryEnabled")}>
              <Controller
                control={form.control}
                name="memory.enabled"
                render={({ field }) => (
                  <Switch checked={field.value} onCheckedChange={field.onChange} />
                )}
              />
            </Field>
            {memoryEnabled ? (
              <Field
                label={t("forms.llm.windowTurns")}
                error={errorAt(form, "memory.window_turns", serverIssue)}
              >
                <Input
                  type="number"
                  step="1"
                  className="w-28"
                  {...form.register("memory.window_turns", { setValueAs: toNumber })}
                />
              </Field>
            ) : null}
          </>
        ) : (
          <p className="text-xs text-ink-muted">{t("forms.llm.memoryWorkflow")}</p>
        )}
      </Section>
      <Section title={t("forms.llm.vision")}>
        <Field inline label={t("forms.llm.visionEnabled")}>
          <Controller
            control={form.control}
            name="vision.enabled"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
        {visionEnabled ? (
          <Field label={t("forms.llm.visionVar")} error={errorAt(form, "vision.var", serverIssue)}>
            <Controller
              control={form.control}
              name="vision.var"
              render={({ field }) => (
                <VariablePicker
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  accept={["file", "array[file]"]}
                />
              )}
            />
          </Field>
        ) : null}
      </Section>
      <Section title={t("forms.llm.json")}>
        <Field inline label={t("forms.llm.jsonEnabled")} hint={t("forms.llm.jsonHint")}>
          <Controller
            control={form.control}
            name="json_mode"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
        {jsonMode ? (
          <Field
            label={t("forms.llm.outputSchema")}
            hint={t("forms.llm.outputSchemaHint")}
            error={errorAt(form, "output_schema_text", serverIssue) ?? serverIssue("output_schema")}
          >
            <Controller
              control={form.control}
              name="output_schema_text"
              render={({ field }) => (
                <CodeEditor
                  value={field.value}
                  onChange={field.onChange}
                  language="json"
                  ariaLabel={t("forms.llm.outputSchema")}
                  minHeight={120}
                  maxHeight={320}
                />
              )}
            />
          </Field>
        ) : serverIssue("output_schema") ? (
          <Alert tone="warn" title={serverIssue("output_schema")} />
        ) : null}
      </Section>
    </div>
  );
}
