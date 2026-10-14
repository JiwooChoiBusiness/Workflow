import { useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import {
  templateFromForm,
  templateSchema,
  templateToForm,
  type TemplateForm as TemplateFormValues,
} from "@/features/workflows/configSchemas";
import type { VarGroup } from "@/features/workflows/useUpstream";
import { PromptEditor } from "../PromptEditor";
import { NameVarRows } from "./NameVarRows";
import { errorAt, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

export function TemplateForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => templateSchema(tc, t), [tc, t]);
  const form = useNodeForm<TemplateFormValues>(node, schema, templateToForm, templateFromForm);
  const serverIssue = useServerIssue(node.id);
  const vars = useWatch({ control: form.control, name: "vars" });
  // template 노드는 변수 풀이 아니라 vars의 이름만 템플릿에서 쓴다(서버 TemplateRunner).
  const groups = useMemo<VarGroup[]>(
    () => [
      {
        key: "vars",
        scope: "vars",
        nodeType: null,
        title: t("forms.template.vars"),
        vars: (vars ?? [])
          .filter((item) => item.name.trim())
          .map((item) => ({ selector: item.name.trim(), name: item.name.trim(), type: "any" })),
      },
    ],
    [vars, t],
  );
  return (
    <div className="flex flex-col gap-4">
      <NameVarRows
        form={form}
        name="vars"
        nodeId={node.id}
        serverIssue={serverIssue}
        title={t("forms.template.vars")}
        hint={t("forms.template.varsHint")}
        addLabel={t("forms.template.add")}
      />
      <Field
        label={t("forms.template.template")}
        hint={t("forms.template.templateHint")}
        error={errorAt(form, "template", serverIssue)}
      >
        <Controller
          control={form.control}
          name="template"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              groups={groups}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.template.template")}
              minHeight={160}
            />
          )}
        />
      </Field>
    </div>
  );
}
