import { useMemo } from "react";
import { Controller } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import {
  answerFromForm,
  answerSchema,
  answerToForm,
  type AnswerForm as AnswerFormValues,
} from "@/features/workflows/configSchemas";
import { PromptEditor } from "../PromptEditor";
import { errorAt, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

export function AnswerForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const schema = useMemo(() => answerSchema(), []);
  const form = useNodeForm<AnswerFormValues>(node, schema, answerToForm, answerFromForm);
  const serverIssue = useServerIssue(node.id);
  return (
    <Field
      label={t("forms.answer.text")}
      hint={t("forms.answer.hint", { example: "{{ llm_1.text }}" })}
      error={errorAt(form, "text", serverIssue)}
    >
      <Controller
        control={form.control}
        name="text"
        render={({ field }) => (
          <PromptEditor
            nodeId={node.id}
            value={field.value}
            onChange={field.onChange}
            ariaLabel={t("forms.answer.text")}
            minHeight={120}
          />
        )}
      />
    </Field>
  );
}
