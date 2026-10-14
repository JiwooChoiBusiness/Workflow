import { Controller, useFieldArray, type FieldValues, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { VarType } from "@/api/types";
import { Input } from "@/components/ui/input";
import { VariablePicker } from "../VariablePicker";
import { AddButton, EmptyRows, errorAt, FieldMessage, RemoveButton, Section } from "./shared";

type NameVarFields = Record<string, { name: string; var: string }[]>;

// 이름 + 변수 선택 목록(end outputs, template vars, code inputs, agent inputs). 값은 {name, var}[]다.
export function NameVarRows<F extends FieldValues>({
  form: anyForm,
  name,
  nodeId,
  serverIssue,
  title,
  hint,
  addLabel,
  empty,
  accept,
}: {
  form: UseFormReturn<F>;
  name: string;
  nodeId: string;
  serverIssue: (path: string) => string | undefined;
  title: string;
  hint?: string;
  addLabel: string;
  empty?: string;
  accept?: readonly VarType[];
}) {
  const { t } = useTranslation("workflows");
  const form = anyForm as unknown as UseFormReturn<NameVarFields>;
  const { fields, append, remove } = useFieldArray({ control: form.control, name });
  const listError = errorAt(form, name, serverIssue);
  return (
    <Section
      title={title}
      hint={hint}
      actions={<AddButton label={addLabel} onClick={() => append({ name: "", var: "" })} />}
    >
      {fields.length === 0 && empty ? <EmptyRows>{empty}</EmptyRows> : null}
      {fields.map((field, index) => {
        const nameError = errorAt(form, `${name}.${index}.name`, serverIssue);
        const varError = errorAt(form, `${name}.${index}.var`, serverIssue);
        const row = t("common.row", { n: index + 1 });
        return (
          <div key={field.id} className="flex flex-col gap-1">
            <div className="flex items-center gap-1.5">
              <Input
                className="w-28 shrink-0 font-mono"
                placeholder={t("common.name")}
                aria-label={`${title} ${row} ${t("common.name")}`}
                aria-invalid={nameError ? true : undefined}
                {...form.register(`${name}.${index}.name`)}
              />
              <Controller
                control={form.control}
                name={`${name}.${index}.var`}
                render={({ field: control }) => (
                  <VariablePicker
                    nodeId={nodeId}
                    value={control.value ?? ""}
                    onChange={control.onChange}
                    accept={accept}
                    aria-label={`${title} ${row} ${t("common.variable")}`}
                    aria-invalid={varError ? true : undefined}
                    className="min-w-0 flex-1"
                  />
                )}
              />
              <RemoveButton
                label={t("common.remove", { name: row })}
                onClick={() => remove(index)}
              />
            </div>
            <FieldMessage message={nameError ?? varError} />
          </div>
        );
      })}
      <FieldMessage message={listError} />
    </Section>
  );
}
