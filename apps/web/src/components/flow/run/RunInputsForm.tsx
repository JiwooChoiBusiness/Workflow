import { Paperclip, X } from "lucide-react";
import { useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { FlowField, WfFileOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import "@/features/workflows/i18n";
import {
  initialFieldValue,
  uploadedOf,
  type FieldValue,
  type UploadedFile,
} from "@/features/workflows/runInputs";
import { errorMessage } from "@/lib/errors";
import { formatBytes } from "@/lib/format";

const UNSET = "__unset__";

function FileField({
  field,
  files,
  disabled,
  upload,
  onChange,
}: {
  field: FlowField;
  files: UploadedFile[];
  disabled: boolean;
  upload: (file: File) => Promise<WfFileOut>;
  onChange: (files: UploadedFile[]) => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const many = field.type === "file_list";
  const id = useId();

  async function pick(list: FileList | null) {
    if (!list || list.length === 0) return;
    setBusy(true);
    setError(null);
    const added: UploadedFile[] = [];
    try {
      for (const file of many ? Array.from(list) : [list[0]!]) {
        added.push(uploadedOf(await upload(file)));
      }
    } catch (failure) {
      setError(errorMessage(failure, tc));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
    if (added.length > 0) onChange(many ? [...files, ...added] : added.slice(0, 1));
  }

  return (
    <div className="flex flex-col gap-1.5">
      {files.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {files.map((file) => (
            <li
              key={file.id}
              className="flex items-center gap-2 rounded-md border border-line bg-sunken/60 px-2 py-1 text-sm"
            >
              <Paperclip className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
              <span className="min-w-0 flex-1 truncate text-ink">{file.filename}</span>
              <span className="shrink-0 text-xs text-ink-subtle tabular-nums">
                {formatBytes(file.size)}
              </span>
              <Button
                size="iconSm"
                variant="ghost"
                disabled={disabled}
                aria-label={t("run.removeFile", { name: file.filename })}
                onClick={() => onChange(files.filter((item) => item.id !== file.id))}
              >
                <X className="size-3.5" aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
      {many || files.length === 0 ? (
        <div>
          <input
            ref={input}
            id={id}
            type="file"
            multiple={many}
            className="sr-only"
            aria-label={field.label || field.name}
            disabled={disabled || busy}
            onChange={(event) => void pick(event.target.files)}
          />
          <Button
            size="sm"
            loading={busy}
            disabled={disabled}
            onClick={() => input.current?.click()}
          >
            <Paperclip className="size-3.5" aria-hidden />
            {busy ? t("run.uploading") : t("run.upload")}
          </Button>
        </div>
      ) : null}
      {error ? <p className="text-xs text-danger">{error}</p> : null}
    </div>
  );
}

/*
 * start 필드로 만든 런 입력 폼(설계 7.3 RunInputsForm, 7.4 FlowRunForm). 파일 칸은 고르는 즉시 올리고 받은
 * id를 값으로 둔다. 업로드 경로는 쓰는 곳(콘솔·웹앱)이 넘긴다.
 */
export function RunInputsForm({
  fields,
  values,
  onChange,
  errors = {},
  upload,
  disabled = false,
}: {
  fields: readonly FlowField[];
  values: Readonly<Record<string, FieldValue>>;
  onChange: (name: string, value: FieldValue) => void;
  errors?: Readonly<Record<string, string>>;
  upload: (file: File) => Promise<WfFileOut>;
  disabled?: boolean;
}) {
  const { t } = useTranslation("workflows");
  if (fields.length === 0) {
    return <p className="text-sm text-ink-muted">{t("run.noInputs")}</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      {fields.map((field) => {
        const value = values[field.name] ?? initialFieldValue(field);
        const label = field.label || field.name;
        const text = typeof value === "string" ? value : "";
        let control;
        switch (field.type) {
          case "paragraph":
            control = (
              <Textarea
                rows={3}
                value={text}
                disabled={disabled}
                maxLength={field.max_length ?? undefined}
                onChange={(event) => onChange(field.name, event.target.value)}
              />
            );
            break;
          case "number":
            control = (
              <Input
                type="number"
                inputMode="decimal"
                value={text}
                disabled={disabled}
                onChange={(event) => onChange(field.name, event.target.value)}
              />
            );
            break;
          case "boolean":
            control = (
              <Select
                value={typeof value === "boolean" ? String(value) : UNSET}
                disabled={disabled}
                onValueChange={(next) =>
                  onChange(field.name, next === UNSET ? null : next === "true")
                }
                options={[
                  { value: UNSET, label: t("run.boolUnset") },
                  { value: "true", label: t("run.boolTrue") },
                  { value: "false", label: t("run.boolFalse") },
                ]}
              />
            );
            break;
          case "select":
            control = (
              <Select
                value={text || UNSET}
                disabled={disabled}
                onValueChange={(next) => onChange(field.name, next === UNSET ? "" : next)}
                options={[
                  { value: UNSET, label: t("run.selectNone") },
                  ...(field.options ?? []).map((option) => ({ value: option, label: option })),
                ]}
              />
            );
            break;
          case "file":
          case "file_list":
            control = (
              <FileField
                field={field}
                files={Array.isArray(value) ? value : []}
                disabled={disabled}
                upload={upload}
                onChange={(files) => onChange(field.name, files)}
              />
            );
            break;
          default:
            control = (
              <Input
                value={text}
                disabled={disabled}
                maxLength={field.max_length ?? undefined}
                onChange={(event) => onChange(field.name, event.target.value)}
              />
            );
        }
        return (
          <Field
            key={field.name}
            label={label}
            hint={
              label !== field.name ? <span className="font-mono">{field.name}</span> : undefined
            }
            required={field.required}
            error={errors[field.name]}
          >
            {control}
          </Field>
        );
      })}
    </div>
  );
}
