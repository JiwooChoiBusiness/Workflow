import { useCallback, useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { VarType } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  ASSIGN_OP_KEYS,
  ASSIGN_OPS,
  assignerFromForm,
  assignerSchema,
  assignerToForm,
  type AssignerForm as AssignerFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { VariablePicker } from "../VariablePicker";
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

const PICK = "__pick__";

export function AssignerForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const conversationVars = useFlow((state) => state.conversationVars);
  const convTypes = useMemo(() => {
    const types: Record<string, VarType> = {};
    for (const item of conversationVars) types[item.name] = item.type;
    return types;
  }, [conversationVars]);
  // 고정값은 대상 대화 변수의 타입에 맞춰 넣는다. 변환 함수가 그 표를 알아야 해서 여기서 만든다.
  const fromForm = useCallback(
    (values: AssignerFormValues) => assignerFromForm(values, convTypes),
    [convTypes],
  );
  const schema = useMemo(() => assignerSchema(t), [t]);
  const form = useNodeForm<AssignerFormValues>(node, schema, assignerToForm, fromForm);
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "items" });
  const items = useWatch({ control: form.control, name: "items" });
  const targetOptions = [
    { value: PICK, label: t("forms.assigner.target"), disabled: true },
    ...conversationVars.map((item) => ({
      value: `conv.${item.name}`,
      label: `conv.${item.name}`,
      hint: item.type,
    })),
  ];
  const sourceOptions = [
    { value: "var", label: t("forms.assigner.fromVar") },
    { value: "value", label: t("forms.assigner.fromValue") },
  ];

  return (
    <Section
      title={t("forms.assigner.items")}
      actions={
        <AddButton
          label={t("forms.assigner.add")}
          onClick={() =>
            append({ target: "", op: "overwrite", source: "var", var: "", value_text: "" })
          }
        />
      }
    >
      {conversationVars.length === 0 ? (
        <Alert tone="warn" title={t("forms.assigner.noConv")} />
      ) : null}
      {fields.length === 0 ? <EmptyRows>{t("forms.assigner.empty")}</EmptyRows> : null}
      {fields.map((field, index) => {
        const item = items?.[index];
        const path = `items.${index}` as const;
        const row = t("common.row", { n: index + 1 });
        const errors = [
          errorAt(form, `${path}.target`, serverIssue),
          errorAt(form, `${path}.op`, serverIssue),
          errorAt(form, `${path}.var`, serverIssue),
          errorAt(form, `${path}.value_text`, serverIssue),
          serverIssue(path),
        ].filter(Boolean);
        return (
          <div key={field.id} className="flex flex-col gap-2 rounded-md border border-line p-2.5">
            <div className="flex items-start gap-1.5">
              <Field className="min-w-0 flex-1" label={t("forms.assigner.target")}>
                <Controller
                  control={form.control}
                  name={`${path}.target`}
                  render={({ field: control }) => (
                    <Select
                      value={control.value || PICK}
                      onValueChange={control.onChange}
                      options={
                        control.value && !targetOptions.some((o) => o.value === control.value)
                          ? [...targetOptions, { value: control.value, label: control.value }]
                          : targetOptions
                      }
                    />
                  )}
                />
              </Field>
              <Field className="w-36 shrink-0" label={t("forms.assigner.op")}>
                <Controller
                  control={form.control}
                  name={`${path}.op`}
                  render={({ field: control }) => (
                    <Select
                      value={control.value}
                      onValueChange={control.onChange}
                      options={ASSIGN_OPS.map((op) => ({
                        value: op,
                        label: t(`forms.assigner.ops.${ASSIGN_OP_KEYS[op]}`),
                      }))}
                    />
                  )}
                />
              </Field>
              <div className="pt-6">
                <RemoveButton
                  label={t("common.remove", { name: row })}
                  onClick={() => remove(index)}
                />
              </div>
            </div>
            {item?.op === "clear" ? null : (
              <div className="flex items-center gap-1.5">
                <Controller
                  control={form.control}
                  name={`${path}.source`}
                  render={({ field: control }) => (
                    <Select
                      aria-label={t("forms.assigner.source")}
                      className="w-24 shrink-0"
                      value={control.value}
                      onValueChange={control.onChange}
                      options={sourceOptions}
                    />
                  )}
                />
                {item?.source === "value" ? (
                  <Input
                    className="min-w-0 flex-1 font-mono"
                    aria-label={`${row} ${t("forms.assigner.fromValue")}`}
                    title={t("forms.assigner.valueHint")}
                    {...form.register(`${path}.value_text`)}
                  />
                ) : (
                  <Controller
                    control={form.control}
                    name={`${path}.var`}
                    render={({ field: control }) => (
                      <VariablePicker
                        nodeId={node.id}
                        value={control.value}
                        onChange={control.onChange}
                        aria-label={`${row} ${t("forms.assigner.fromVar")}`}
                        className="min-w-0 flex-1"
                      />
                    )}
                  />
                )}
              </div>
            )}
            {errors.map((message) => (
              <FieldMessage key={message} message={message} />
            ))}
          </div>
        );
      })}
      <FieldMessage message={errorAt(form, "items", serverIssue)} />
    </Section>
  );
}
