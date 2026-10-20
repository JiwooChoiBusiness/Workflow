import { useMemo } from "react";
import { Controller, useFieldArray } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { Select } from "@/components/ui/select";
import {
  codeFromForm,
  codeSchema,
  codeToForm,
  VAR_TYPE_OPTIONS,
  type CodeForm as CodeFormValues,
} from "@/features/workflows/configSchemas";
import { isImportedCode, withoutImportedCode } from "@/features/workflows/dsl";
import { useFlowActions } from "@/features/workflows/flowStoreContext";
import type { FlowNode } from "@/features/workflows/graph";
import { toNumberOrNull } from "@/lib/formSchemas";
import { NameVarRows } from "./NameVarRows";
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

// DSL로 가져온 코드(설계 9.3). 검토 완료는 ui의 imported_code 키만 지우고 저장은 자동 저장에 맡긴다.
function ImportedCodeReview({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  return (
    <Alert
      tone="warn"
      title={t("dsl.review.title")}
      action={
        <Button
          size="sm"
          onClick={() => actions.updateNode(node.id, { ui: withoutImportedCode(node.ui) })}
        >
          {t("dsl.review.done")}
        </Button>
      }
    >
      {t("dsl.review.body")}
    </Alert>
  );
}

export function CodeForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => codeSchema(tc, t), [tc, t]);
  const form = useNodeForm<CodeFormValues>(node, schema, codeToForm, codeFromForm);
  const serverIssue = useServerIssue(node.id);
  const outputs = useFieldArray({ control: form.control, name: "outputs" });
  const typeOptions = VAR_TYPE_OPTIONS.map((type) => ({ value: type, label: type }));

  return (
    <div className="flex flex-col gap-4">
      {isImportedCode(node) ? <ImportedCodeReview node={node} /> : null}
      <NameVarRows
        form={form}
        name="inputs"
        nodeId={node.id}
        serverIssue={serverIssue}
        title={t("forms.code.inputs")}
        hint={t("forms.code.inputsHint")}
        addLabel={t("common.add")}
      />
      <Field
        label={t("forms.code.code")}
        hint={t("forms.code.codeHint")}
        error={errorAt(form, "code", serverIssue)}
      >
        <Controller
          control={form.control}
          name="code"
          render={({ field }) => (
            <CodeEditor
              value={field.value}
              onChange={field.onChange}
              language="python"
              ariaLabel={t("forms.code.code")}
              minHeight={200}
              maxHeight={480}
            />
          )}
        />
      </Field>
      <Section
        title={t("forms.code.outputs")}
        hint={t("forms.code.outputsHint")}
        actions={
          <AddButton
            label={t("common.add")}
            onClick={() => outputs.append({ name: "", type: "string" })}
          />
        }
      >
        {outputs.fields.map((field, index) => {
          const row = t("common.row", { n: index + 1 });
          return (
            <div key={field.id} className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5">
                <Input
                  className="min-w-0 flex-1 font-mono"
                  placeholder={t("common.name")}
                  aria-label={`${t("forms.code.outputs")} ${row} ${t("common.name")}`}
                  {...form.register(`outputs.${index}.name`)}
                />
                <Controller
                  control={form.control}
                  name={`outputs.${index}.type`}
                  render={({ field: control }) => (
                    <Select
                      aria-label={`${t("forms.code.outputs")} ${row} ${t("common.type")}`}
                      className="w-36 shrink-0"
                      value={control.value}
                      onValueChange={control.onChange}
                      options={typeOptions}
                    />
                  )}
                />
                <RemoveButton
                  label={t("common.remove", { name: row })}
                  onClick={() => outputs.remove(index)}
                />
              </div>
              <FieldMessage message={errorAt(form, `outputs.${index}.name`, serverIssue)} />
            </div>
          );
        })}
      </Section>
      <Field
        label={t("forms.code.timeout")}
        hint={t("forms.code.timeoutHint")}
        error={errorAt(form, "timeout_sec", serverIssue)}
      >
        <Input
          type="number"
          step="1"
          className="w-28"
          {...form.register("timeout_sec", { setValueAs: toNumberOrNull })}
        />
      </Field>
    </div>
  );
}
