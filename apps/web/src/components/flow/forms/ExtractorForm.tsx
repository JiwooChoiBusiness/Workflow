import { useMemo } from "react";
import { Controller, useFieldArray } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/toggle";
import {
  extractorFromForm,
  extractorSchema,
  extractorToForm,
  newExtractParam,
  PARAM_TYPES,
  type ExtractorForm as ExtractorFormValues,
} from "@/features/workflows/configSchemas";
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

export function ExtractorForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => extractorSchema(tc, t), [tc, t]);
  const form = useNodeForm<ExtractorFormValues>(node, schema, extractorToForm, extractorFromForm);
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "parameters" });
  const typeOptions = PARAM_TYPES.map((type) => ({ value: type, label: type }));

  return (
    <div className="flex flex-col gap-4">
      <ModelSection form={form} />
      <Field label={t("forms.extractor.query")} error={errorAt(form, "query_var", serverIssue)}>
        <Controller
          control={form.control}
          name="query_var"
          render={({ field }) => (
            <VariablePicker
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              accept={["string"]}
            />
          )}
        />
      </Field>
      <Section
        title={t("forms.extractor.parameters")}
        actions={
          <AddButton label={t("forms.extractor.add")} onClick={() => append(newExtractParam())} />
        }
      >
        {fields.map((field, index) => {
          const path = `parameters.${index}` as const;
          const row = t("common.row", { n: index + 1 });
          return (
            <div
              key={field.id}
              className="flex flex-col gap-1.5 rounded-md border border-line p-2.5"
            >
              <div className="flex items-center gap-1.5">
                <Input
                  className="min-w-0 flex-1 font-mono"
                  placeholder={t("common.name")}
                  aria-label={`${row} ${t("common.name")}`}
                  {...form.register(`${path}.name`)}
                />
                <Controller
                  control={form.control}
                  name={`${path}.type`}
                  render={({ field: control }) => (
                    <Select
                      aria-label={`${row} ${t("common.type")}`}
                      className="w-36 shrink-0"
                      value={control.value}
                      onValueChange={control.onChange}
                      options={typeOptions}
                    />
                  )}
                />
                <RemoveButton
                  label={t("common.remove", { name: row })}
                  onClick={() => remove(index)}
                />
              </div>
              <Input
                placeholder={t("common.description")}
                aria-label={`${row} ${t("common.description")}`}
                {...form.register(`${path}.description`)}
              />
              <div className="flex items-center gap-3">
                <label className="flex shrink-0 items-center gap-2 text-sm text-ink">
                  <Controller
                    control={form.control}
                    name={`${path}.required`}
                    render={({ field: control }) => (
                      <Checkbox
                        checked={control.value}
                        onCheckedChange={(checked) => control.onChange(checked === true)}
                      />
                    )}
                  />
                  {t("common.required")}
                </label>
                <Input
                  className="min-w-0 flex-1"
                  placeholder={t("forms.extractor.enum")}
                  title={t("forms.extractor.enumHint")}
                  aria-label={`${row} ${t("forms.extractor.enum")}`}
                  {...form.register(`${path}.enum_text`)}
                />
              </div>
              <FieldMessage
                message={
                  errorAt(form, `${path}.name`, serverIssue) ??
                  errorAt(form, `${path}.enum_text`, serverIssue) ??
                  serverIssue(`${path}.enum`)
                }
              />
            </div>
          );
        })}
        <FieldMessage message={errorAt(form, "parameters", serverIssue)} />
      </Section>
      <Field label={t("forms.extractor.instruction")} error={serverIssue("instruction")}>
        <Controller
          control={form.control}
          name="instruction"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.extractor.instruction")}
            />
          )}
        />
      </Field>
      <Field label={t("forms.extractor.reasoningMode")}>
        <Controller
          control={form.control}
          name="reasoning_mode"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={(["tool_call", "prompt"] as const).map((value) => ({
                value,
                label: t(`forms.extractor.modes.${value}`),
              }))}
            />
          )}
        />
      </Field>
    </div>
  );
}
