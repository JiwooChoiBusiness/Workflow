import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { DocumentMetadataValue, MetadataFieldOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { Checkbox, Switch } from "@/components/ui/toggle";
import { useUpdateDocumentMetadata } from "@/features/knowledge/api";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatDateTime } from "@/lib/format";

interface Draft {
  text: string;
  clear: boolean;
}

// <input type="datetime-local">은 시간대 없는 현지 시각이다. 서버는 시간대가 있는 ISO 8601만 받는다.
function toLocalInput(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function initialText(field: MetadataFieldOut, current: DocumentMetadataValue | undefined): string {
  const value = current?.value;
  if (value === null || value === undefined) return "";
  if (field.type === "time") return toLocalInput(String(value));
  return String(value);
}

type Converted = { ok: true; value: string | number | null } | { ok: false };

function convert(field: MetadataFieldOut, draft: Draft): Converted {
  if (draft.clear) return { ok: true, value: null };
  if (field.type === "number") {
    const number = Number(draft.text);
    return draft.text.trim() !== "" && Number.isFinite(number)
      ? { ok: true, value: number }
      : { ok: false };
  }
  if (field.type === "time") {
    const date = new Date(draft.text);
    return Number.isNaN(date.getTime()) ? { ok: false } : { ok: true, value: date.toISOString() };
  }
  return { ok: true, value: draft.text };
}

function ValueInput({
  field,
  draft,
  onChange,
  error,
}: {
  field: MetadataFieldOut;
  draft: Draft;
  onChange: (draft: Draft) => void;
  error?: string;
}) {
  const { t } = useTranslation("knowledge");
  return (
    <Field
      label={
        <span className="inline-flex items-center gap-1.5">
          <span className="font-mono">{field.name}</span>
          <Badge>{t(`metadata.types.${field.type}`)}</Badge>
        </span>
      }
      error={error}
    >
      <div className="flex items-center gap-2">
        <Input
          type={
            field.type === "number" ? "number" : field.type === "time" ? "datetime-local" : "text"
          }
          value={draft.text}
          disabled={draft.clear}
          className="flex-1"
          onChange={(event) => onChange({ text: event.target.value, clear: false })}
        />
        <Field label={t("metadata.clear")} inline className="shrink-0 items-center">
          <Checkbox
            checked={draft.clear}
            onCheckedChange={(checked) => onChange({ ...draft, clear: checked === true })}
          />
        </Field>
      </div>
    </Field>
  );
}

function MetadataForm({
  kbId,
  documentIds,
  fields,
  current,
  bulk,
  onDone,
  onCancel,
}: {
  kbId: string;
  documentIds: string[];
  fields: MetadataFieldOut[];
  current?: DocumentMetadataValue[];
  bulk: boolean;
  onDone?: () => void;
  onCancel?: () => void;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const update = useUpdateDocumentMetadata(kbId);
  const editable = fields.filter((field) => !field.built_in);
  const builtIn = fields.filter((field) => field.built_in);
  const byField = new Map((current ?? []).map((item) => [item.field_id, item]));
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() =>
    Object.fromEntries(
      editable.map((field) => [
        field.id,
        { text: initialText(field, byField.get(field.id)), clear: false },
      ]),
    ),
  );
  const [applyToAll, setApplyToAll] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);

  function save() {
    const values: { field_id: string; value: string | number | null }[] = [];
    const found: Record<string, string> = {};
    for (const field of editable) {
      const draft = drafts[field.id];
      const before = initialText(field, byField.get(field.id));
      const touched = draft.clear || draft.text !== before;
      if (!touched || (!draft.clear && draft.text === "" && bulk)) continue;
      if (!draft.clear && draft.text === "") {
        values.push({ field_id: field.id, value: null });
        continue;
      }
      const converted = convert(field, draft);
      if (!converted.ok) {
        found[field.id] = t("metadata.numberInvalid");
        continue;
      }
      values.push({ field_id: field.id, value: converted.value });
    }
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    if (values.length === 0) {
      setProblem(t("metadata.nothingChanged"));
      return;
    }
    setProblem(null);
    update.mutate(
      { document_ids: documentIds, values, apply_to_all: bulk ? applyToAll : true },
      {
        onSuccess: () => {
          toast.success(t("metadata.saved"));
          onDone?.();
        },
        onError: (error) => setProblem(errorText(error)),
      },
    );
  }

  if (editable.length === 0 && builtIn.length === 0) {
    return <p className="text-sm text-ink-muted">{t("metadata.noFields")}</p>;
  }

  return (
    <form
      className="flex flex-col gap-3"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      {builtIn.length > 0 && !bulk ? (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
          {builtIn.map((field) => {
            const value = byField.get(field.id)?.value;
            return (
              <div key={field.id} className="contents">
                <dt className="font-mono text-ink-muted">{field.name}</dt>
                <dd className="text-ink">
                  {value === null || value === undefined
                    ? t("metadata.empty_value")
                    : field.type === "time"
                      ? formatDateTime(String(value))
                      : String(value)}
                </dd>
              </div>
            );
          })}
        </dl>
      ) : null}
      {editable.length === 0 ? (
        <p className="text-sm text-ink-muted">{t("metadata.noFields")}</p>
      ) : (
        <>
          <p className="text-xs text-ink-muted">{t("metadata.valuesHint")}</p>
          {editable.map((field) => (
            <ValueInput
              key={field.id}
              field={field}
              draft={drafts[field.id]}
              error={errors[field.id]}
              onChange={(draft) => setDrafts((all) => ({ ...all, [field.id]: draft }))}
            />
          ))}
          {bulk ? (
            <Field label={t("metadata.applyToAll")} hint={t("metadata.applyToAllHint")} inline>
              <Switch checked={applyToAll} onCheckedChange={setApplyToAll} />
            </Field>
          ) : null}
          {problem ? <Alert tone="danger" title={problem} /> : null}
          <DialogFooter className="mt-1">
            {onCancel ? (
              <Button variant="secondary" onClick={onCancel}>
                {tc("actions.cancel")}
              </Button>
            ) : null}
            <Button type="submit" variant="primary" loading={update.isPending}>
              {tc("actions.save")}
            </Button>
          </DialogFooter>
        </>
      )}
    </form>
  );
}

// 문서 하나의 메타데이터 값(문서 화면).
export function MetadataEditor({
  kbId,
  documentId,
  fields,
  current,
}: {
  kbId: string;
  documentId: string;
  fields: MetadataFieldOut[];
  current: DocumentMetadataValue[];
}) {
  return (
    <MetadataForm
      key={JSON.stringify(current)}
      kbId={kbId}
      documentIds={[documentId]}
      fields={fields}
      current={current}
      bulk={false}
    />
  );
}

// 문서 표에서 고른 여러 문서의 값을 한꺼번에(설계 7.5 일괄 편집). 비운 칸은 바꾸지 않는다.
export function BulkMetadataDialog({
  kbId,
  documentIds,
  fields,
  open,
  onOpenChange,
}: {
  kbId: string;
  documentIds: string[];
  fields: MetadataFieldOut[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("knowledge");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={
          documentIds.length > 1
            ? t("metadata.editTitleMany", { count: documentIds.length })
            : t("metadata.editTitle")
        }
        className="w-[min(36rem,calc(100vw-2rem))]"
      >
        {open ? (
          <MetadataForm
            kbId={kbId}
            documentIds={documentIds}
            fields={fields}
            bulk
            onDone={() => onOpenChange(false)}
            onCancel={() => onOpenChange(false)}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
