import { useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/toggle";
import {
  newStartField,
  START_FIELD_TYPES,
  startFromForm,
  startSchema,
  startToForm,
  type StartForm as StartFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { toNumberOrNull } from "@/lib/formSchemas";
import {
  AddButton,
  EmptyRows,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

const NONE = "__none__";

export function StartForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const kind = useFlow((state) => state.kind);
  const schema = useMemo(() => startSchema(tc, t), [tc, t]);
  const form = useNodeForm<StartFormValues>(node, schema, startToForm, startFromForm);
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "fields" });
  const values = useWatch({ control: form.control, name: "fields" });
  const typeOptions = START_FIELD_TYPES.map((type) => ({
    value: type,
    label: t(`forms.start.types.${type}`),
  }));

  return (
    <Section
      title={t("forms.start.fields")}
      actions={<AddButton label={t("forms.start.add")} onClick={() => append(newStartField())} />}
    >
      {kind === "chatflow" ? <Alert tone="info" title={t("forms.start.chatflowHint")} /> : null}
      {fields.length === 0 ? <EmptyRows>{t("forms.start.empty")}</EmptyRows> : null}
      {fields.map((field, index) => {
        const row = values?.[index];
        const type = row?.type ?? "string";
        const path = `fields.${index}` as const;
        const label = row?.name || t("common.row", { n: index + 1 });
        return (
          <div key={field.id} className="flex flex-col gap-2 rounded-md border border-line p-2.5">
            <div className="flex items-start gap-1.5">
              <Field
                className="min-w-0 flex-1"
                label={t("forms.start.name")}
                error={errorAt(form, `${path}.name`, serverIssue)}
                required
              >
                <Input className="font-mono" {...form.register(`${path}.name`)} />
              </Field>
              <Field className="w-32 shrink-0" label={t("common.type")}>
                <Controller
                  control={form.control}
                  name={`${path}.type`}
                  render={({ field: control }) => (
                    <Select
                      value={control.value}
                      onValueChange={control.onChange}
                      options={typeOptions}
                    />
                  )}
                />
              </Field>
              <div className="pt-6">
                <RemoveButton
                  label={t("common.remove", { name: label })}
                  onClick={() => remove(index)}
                />
              </div>
            </div>
            <Field label={t("forms.start.label")}>
              <Input {...form.register(`${path}.label`)} />
            </Field>
            {type === "select" ? (
              <Field
                label={t("forms.start.options")}
                error={
                  errorAt(form, `${path}.options_text`, serverIssue) ??
                  serverIssue(`${path}.options`)
                }
              >
                <Textarea rows={3} {...form.register(`${path}.options_text`)} />
              </Field>
            ) : null}
            {type === "file" || type === "file_list" ? null : type === "boolean" ? (
              <Field label={t("forms.start.default")}>
                <Controller
                  control={form.control}
                  name={`${path}.default_text`}
                  render={({ field: control }) => (
                    <Select
                      value={control.value || NONE}
                      onValueChange={(value) => control.onChange(value === NONE ? "" : value)}
                      options={[
                        { value: NONE, label: t("forms.start.boolNone") },
                        { value: "true", label: "true" },
                        { value: "false", label: "false" },
                      ]}
                    />
                  )}
                />
              </Field>
            ) : (
              <Field
                label={t("forms.start.default")}
                error={
                  errorAt(form, `${path}.default_text`, serverIssue) ??
                  serverIssue(`${path}.default`)
                }
              >
                <Input {...form.register(`${path}.default_text`)} />
              </Field>
            )}
            {type === "string" || type === "paragraph" ? (
              <Field
                label={t("forms.start.maxLength")}
                error={errorAt(form, `${path}.max_length`, serverIssue)}
              >
                <Input
                  type="number"
                  step="1"
                  {...form.register(`${path}.max_length`, { setValueAs: toNumberOrNull })}
                />
              </Field>
            ) : null}
            <label className="flex items-center gap-2 text-sm text-ink">
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
          </div>
        );
      })}
      <FieldMessage message={errorAt(form, "fields", serverIssue)} />
    </Section>
  );
}
