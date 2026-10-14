import { useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  classifierFromForm,
  classifierSchema,
  classifierToForm,
  type ClassifierForm as ClassifierFormValues,
} from "@/features/workflows/configSchemas";
import { nextItemId } from "@/features/workflows/graph";
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

// 클래스 id는 핸들 이름(class_<id>)이라 자동으로 붙이고 바꾸지 않는다.
export function ClassifierForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => classifierSchema(tc, t), [tc, t]);
  const form = useNodeForm<ClassifierFormValues>(
    node,
    schema,
    classifierToForm,
    classifierFromForm,
  );
  const serverIssue = useServerIssue(node.id);
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "classes",
    keyName: "key",
  });
  const classes = useWatch({ control: form.control, name: "classes" });

  return (
    <div className="flex flex-col gap-4">
      <ModelSection form={form} />
      <Field label={t("forms.classifier.query")} error={errorAt(form, "query_var", serverIssue)}>
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
        title={t("forms.classifier.classes")}
        actions={
          <AddButton
            label={t("forms.classifier.add")}
            onClick={() => {
              const id = nextItemId((classes ?? []).map((item) => item.id));
              append({
                id,
                name: t("forms.classifier.defaultName", { n: (classes?.length ?? 0) + 1 }),
                description: "",
              });
            }}
          />
        }
      >
        {fields.map((field, index) => {
          const item = classes?.[index];
          const name = item?.name || t("common.row", { n: index + 1 });
          return (
            <div
              key={field.key}
              className="flex flex-col gap-1.5 rounded-md border border-line p-2.5"
            >
              <div className="flex items-center gap-1.5">
                <Badge className="font-mono">class_{item?.id}</Badge>
                <Input
                  className="min-w-0 flex-1"
                  aria-label={`${name} ${t("forms.classifier.className")}`}
                  placeholder={t("forms.classifier.className")}
                  {...form.register(`classes.${index}.name`)}
                />
                <RemoveButton
                  label={t("forms.classifier.remove", { name })}
                  onClick={() => remove(index)}
                />
              </div>
              <Textarea
                rows={2}
                aria-label={`${name} ${t("forms.classifier.classDescription")}`}
                placeholder={t("forms.classifier.classDescription")}
                {...form.register(`classes.${index}.description`)}
              />
              <FieldMessage
                message={
                  errorAt(form, `classes.${index}.name`, serverIssue) ??
                  errorAt(form, `classes.${index}.description`, serverIssue)
                }
              />
            </div>
          );
        })}
        <FieldMessage message={errorAt(form, "classes", serverIssue)} />
      </Section>
      <Field label={t("forms.classifier.instruction")} error={serverIssue("instruction")}>
        <Controller
          control={form.control}
          name="instruction"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.classifier.instruction")}
            />
          )}
        />
      </Field>
      <Field label={t("forms.classifier.onParseFailure")}>
        <Controller
          control={form.control}
          name="on_parse_failure"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={(["first_class", "fail"] as const).map((value) => ({
                value,
                label: t(`forms.classifier.failures.${value}`),
              }))}
            />
          )}
        />
      </Field>
    </div>
  );
}
