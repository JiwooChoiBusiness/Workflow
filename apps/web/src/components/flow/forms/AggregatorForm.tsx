import { useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import {
  aggregatorFromForm,
  aggregatorSchema,
  aggregatorToForm,
  VAR_TYPE_OPTIONS,
  type AggregatorForm as AggregatorFormValues,
} from "@/features/workflows/configSchemas";
import { VariablePicker } from "../VariablePicker";
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

export function AggregatorForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const schema = useMemo(() => aggregatorSchema(t), [t]);
  const form = useNodeForm<AggregatorFormValues>(
    node,
    schema,
    aggregatorToForm,
    aggregatorFromForm,
  );
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "vars" });
  const outputType = useWatch({ control: form.control, name: "output_type" });
  return (
    <div className="flex flex-col gap-4">
      <Section
        title={t("forms.aggregator.vars")}
        hint={t("forms.aggregator.varsHint")}
        actions={
          <AddButton label={t("forms.aggregator.add")} onClick={() => append({ var: "" })} />
        }
      >
        {fields.map((field, index) => {
          const error =
            errorAt(form, `vars.${index}.var`, serverIssue) ?? serverIssue(`vars.${index}`);
          const row = t("common.row", { n: index + 1 });
          return (
            <div key={field.id} className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5">
                <span className="w-5 shrink-0 text-right text-xs text-ink-subtle">{index + 1}</span>
                <Controller
                  control={form.control}
                  name={`vars.${index}.var`}
                  render={({ field: control }) => (
                    <VariablePicker
                      nodeId={node.id}
                      value={control.value}
                      onChange={control.onChange}
                      accept={outputType === "any" ? undefined : [outputType]}
                      aria-label={`${t("forms.aggregator.vars")} ${row}`}
                      aria-invalid={error ? true : undefined}
                      className="min-w-0 flex-1"
                    />
                  )}
                />
                <RemoveButton
                  label={t("common.remove", { name: row })}
                  onClick={() => remove(index)}
                />
              </div>
              <FieldMessage message={error} />
            </div>
          );
        })}
        <FieldMessage message={errorAt(form, "vars", serverIssue)} />
      </Section>
      <Field label={t("forms.aggregator.outputType")}>
        <Controller
          control={form.control}
          name="output_type"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={VAR_TYPE_OPTIONS.map((type) => ({ value: type, label: type }))}
            />
          )}
        />
      </Field>
    </div>
  );
}
