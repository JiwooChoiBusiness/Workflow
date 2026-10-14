import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  endFromForm,
  endSchema,
  endToForm,
  type EndForm as EndFormValues,
} from "@/features/workflows/configSchemas";
import { NameVarRows } from "./NameVarRows";
import { useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

export function EndForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => endSchema(tc, t), [tc, t]);
  const form = useNodeForm<EndFormValues>(node, schema, endToForm, endFromForm);
  const serverIssue = useServerIssue(node.id);
  return (
    <NameVarRows
      form={form}
      name="outputs"
      nodeId={node.id}
      serverIssue={serverIssue}
      title={t("forms.end.outputs")}
      hint={t("forms.end.hint")}
      addLabel={t("forms.end.add")}
      empty={t("forms.end.empty")}
    />
  );
}
