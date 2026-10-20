import { useMemo } from "react";
import { Controller } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import {
  documentExtractorFromForm,
  documentExtractorSchema,
  documentExtractorToForm,
  type DocumentExtractorForm as DocumentExtractorFormValues,
} from "@/features/workflows/configSchemas";
import { VariablePicker } from "../VariablePicker";
import { errorAt, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

const FILE_TYPES = ["file", "array[file]"] as const;

// document_extractor(설계 2.4): 파일 변수 하나를 고른다. pdf·docx는 서버가 샌드박스 파이썬으로 읽는다.
export function DocumentExtractorForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const schema = useMemo(() => documentExtractorSchema(t), [t]);
  const form = useNodeForm<DocumentExtractorFormValues>(
    node,
    schema,
    documentExtractorToForm,
    documentExtractorFromForm,
  );
  const serverIssue = useServerIssue(node.id);
  return (
    <div className="flex flex-col gap-4">
      <Field
        label={t("forms.documentExtractor.file")}
        hint={t("forms.documentExtractor.fileHint")}
        error={errorAt(form, "file_var", serverIssue)}
      >
        <Controller
          control={form.control}
          name="file_var"
          render={({ field }) => (
            <VariablePicker
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              accept={FILE_TYPES}
            />
          )}
        />
      </Field>
      <p className="rounded-md bg-sunken px-3 py-2 text-xs text-ink-muted">
        {t("forms.documentExtractor.formats")}
      </p>
    </div>
  );
}
