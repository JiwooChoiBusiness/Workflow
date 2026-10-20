import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import {
  startFromForm,
  startSchema,
  startToForm,
  type StartForm as StartFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { useNodeForm, useServerIssue, type NodeFormProps } from "./shared";
import { StartFieldsEditor } from "./StartFieldsEditor";

export function StartForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const kind = useFlow((state) => state.kind);
  const schema = useMemo(() => startSchema(tc, t), [tc, t]);
  const form = useNodeForm<StartFormValues>(node, schema, startToForm, startFromForm);
  const serverIssue = useServerIssue(node.id);
  return (
    <StartFieldsEditor
      form={form}
      serverIssue={serverIssue}
      title={t("forms.start.fields")}
      empty={t("forms.start.empty")}
      before={
        kind === "chatflow" ? <Alert tone="info" title={t("forms.start.chatflowHint")} /> : null
      }
    />
  );
}
