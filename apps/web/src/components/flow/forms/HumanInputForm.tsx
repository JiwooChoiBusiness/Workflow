import { useMemo } from "react";
import { Controller } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import {
  humanInputFromForm,
  humanInputSchema,
  humanInputToForm,
  type HumanInputForm as HumanInputFormValues,
} from "@/features/workflows/configSchemas";
import { PromptEditor } from "../PromptEditor";
import { errorAt, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";
import { StartFieldsEditor } from "./StartFieldsEditor";

/*
 * human_input(설계 2.4): 런이 여기서 멈추고 폼을 띄운다. 안내문은 템플릿이라 앞 노드의 값을 넣어 보일 수
 * 있고, 필드는 시작 노드의 입력 필드와 같은 편집기다. 받은 값은 필드 이름으로 이 노드의 출력이 된다.
 */
export function HumanInputForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => humanInputSchema(tc, t), [tc, t]);
  const form = useNodeForm<HumanInputFormValues>(
    node,
    schema,
    humanInputToForm,
    humanInputFromForm,
  );
  const serverIssue = useServerIssue(node.id);
  return (
    <div className="flex flex-col gap-4">
      <Field
        label={t("forms.humanInput.description")}
        hint={t("forms.humanInput.descriptionHint", { example: "{{ llm_1.text }}" })}
        error={errorAt(form, "description", serverIssue)}
      >
        <Controller
          control={form.control}
          name="description"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.humanInput.description")}
              minHeight={90}
            />
          )}
        />
      </Field>
      <StartFieldsEditor
        form={form}
        serverIssue={serverIssue}
        title={t("forms.humanInput.fields")}
        hint={t("forms.humanInput.fieldsHint")}
        empty={t("forms.humanInput.empty")}
      />
      <p className="rounded-md bg-sunken px-3 py-2 text-xs text-ink-muted">
        {t("forms.humanInput.waitHint")}
      </p>
    </div>
  );
}
