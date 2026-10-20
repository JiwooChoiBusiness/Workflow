import type { ReactNode } from "react";
import {
  Controller,
  useFieldArray,
  useWatch,
  type ArrayPath,
  type FieldArray,
  type FieldValues,
  type Path,
  type UseFormReturn,
} from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/toggle";
import {
  newStartField,
  START_FIELD_TYPES,
  type StartFieldForm,
} from "@/features/workflows/configSchemas";
import { toNumberOrNull } from "@/lib/formSchemas";
import { AddButton, EmptyRows, errorAt, FieldMessage, RemoveButton, Section } from "./shared";

const NONE = "__none__";

/*
 * 입력 필드 목록(이름·종류·표시 이름·선택지·기본값·글자 수·필수). start 노드의 실행 입력과 human_input 노드의
 * 폼(M19)이 같은 모양이라 같이 쓴다. 폼 값의 fields 자리를 고친다.
 */
export function StartFieldsEditor<F extends FieldValues>({
  form,
  serverIssue,
  title,
  hint,
  empty,
  before,
}: {
  form: UseFormReturn<F>;
  serverIssue: (path: string) => string | undefined;
  title: string;
  hint?: string;
  empty: string;
  before?: ReactNode;
}) {
  const { t } = useTranslation("workflows");
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "fields" as ArrayPath<F>,
  });
  const values = useWatch({ control: form.control, name: "fields" as Path<F> }) as
    StartFieldForm[] | undefined;
  const typeOptions = START_FIELD_TYPES.map((type) => ({
    value: type,
    label: t(`forms.start.types.${type}`),
  }));
  const at = (path: string) => path as Path<F>;

  return (
    <Section
      title={title}
      hint={hint}
      actions={
        <AddButton
          label={t("forms.start.add")}
          onClick={() => append(newStartField() as FieldArray<F, ArrayPath<F>>)}
        />
      }
    >
      {before}
      {fields.length === 0 ? <EmptyRows>{empty}</EmptyRows> : null}
      {fields.map((field, index) => {
        const row = values?.[index];
        const type = row?.type ?? "string";
        const path = `fields.${index}`;
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
                <Input className="font-mono" {...form.register(at(`${path}.name`))} />
              </Field>
              <Field className="w-32 shrink-0" label={t("common.type")}>
                <Controller
                  control={form.control}
                  name={at(`${path}.type`)}
                  render={({ field: control }) => (
                    <Select
                      value={control.value as string}
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
              <Input {...form.register(at(`${path}.label`))} />
            </Field>
            {type === "select" ? (
              <Field
                label={t("forms.start.options")}
                error={
                  errorAt(form, `${path}.options_text`, serverIssue) ??
                  serverIssue(`${path}.options`)
                }
              >
                <Textarea rows={3} {...form.register(at(`${path}.options_text`))} />
              </Field>
            ) : null}
            {type === "file" || type === "file_list" ? null : type === "boolean" ? (
              <Field label={t("forms.start.default")}>
                <Controller
                  control={form.control}
                  name={at(`${path}.default_text`)}
                  render={({ field: control }) => (
                    <Select
                      value={(control.value as string) || NONE}
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
                <Input {...form.register(at(`${path}.default_text`))} />
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
                  {...form.register(at(`${path}.max_length`), { setValueAs: toNumberOrNull })}
                />
              </Field>
            ) : null}
            <label className="flex items-center gap-2 text-sm text-ink">
              <Controller
                control={form.control}
                name={at(`${path}.required`)}
                render={({ field: control }) => (
                  <Checkbox
                    checked={control.value as boolean}
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
