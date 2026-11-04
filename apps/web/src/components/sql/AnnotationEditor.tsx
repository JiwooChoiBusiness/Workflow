import { BookUp, TableProperties } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { toast } from "@/components/ui/toast";
import {
  annotationsUpdate,
  hiddenChanged,
  sameDraft,
  type AnnotationDraft,
} from "@/features/sql/annotations";
import { useUpdateAnnotations } from "@/features/sql/api";
import { useSqlError } from "@/features/sql/useSqlError";
import { ExportSchemaDialog } from "./ExportSchemaDialog";
import { SchemaTable } from "./SchemaTable";

const GLOSSARY_MAX = 8000;

function savedDraft(source: SqlSourceOut): AnnotationDraft {
  return { tables: source.annotations.tables ?? {}, glossary: source.glossary };
}

/*
 * 스키마 탭(설계 10.2, 12.3): 표·열 설명과 숨김, 용어집을 고쳐 한 번에 저장한다. 숨김이 바뀌면 서버가 옛 질의
 * 파일(또는 SQLite 사본)을 지우고 다시 준비한다. 다시 준비하지 못하면 stale로 남아 준비 전까지 질의할 수 없다.
 */
export function AnnotationEditor({ source }: { source: SqlSourceOut }) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const update = useUpdateAnnotations(source.id);
  const errorText = useSqlError();
  // null이면 고친 것이 없어 저장된 값을 그대로 보인다(다른 탭에서 준비해 바뀐 값도 바로 따라간다).
  const [draft, setDraft] = useState<AnnotationDraft | null>(null);
  const [exporting, setExporting] = useState(false);
  const saved = savedDraft(source);
  const current = draft ?? saved;
  const dirty = draft !== null && !sameDraft(draft, saved);
  const hiddenDirty = dirty && hiddenChanged(saved.tables, current.tables);
  const tables = source.schema?.tables ?? [];
  const glossaryTooLong = current.glossary.length > GLOSSARY_MAX;

  function save() {
    const changedHidden = hiddenDirty;
    update.mutate(annotationsUpdate(current, true), {
      onSuccess: (next) => {
        setDraft(null);
        if (changedHidden && next.status.state !== "ready") {
          toast.warning(t("schema.notPrepared"));
        } else {
          toast.success(changedHidden ? t("schema.savedPrepared") : t("schema.saved"));
        }
      },
      onError: (error) => toast.error(errorText(error)),
    });
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={t("schema.title")}
        description={t("schema.hint")}
        actions={
          <Button
            size="sm"
            disabled={!source.schema}
            title={t("schema.exportHint")}
            onClick={() => setExporting(true)}
          >
            <BookUp className="size-3.5" aria-hidden />
            {t("schema.export")}
          </Button>
        }
      >
        {tables.length === 0 ? (
          <EmptyState icon={TableProperties} title={t("schema.empty")} />
        ) : (
          <SchemaTable
            tables={tables}
            notes={current.tables}
            onChange={(next) => setDraft({ ...current, tables: next })}
            disabled={update.isPending}
          />
        )}
      </Panel>
      <Panel title={t("schema.glossary")} description={t("schema.glossaryHint")}>
        <Field
          error={glossaryTooLong ? tc("validation.maxLength", { max: GLOSSARY_MAX }) : undefined}
        >
          <Textarea
            rows={4}
            aria-label={t("schema.glossary")}
            value={current.glossary}
            disabled={update.isPending}
            onChange={(event) => setDraft({ ...current, glossary: event.target.value })}
          />
        </Field>
      </Panel>
      {hiddenDirty ? <Alert tone="warn" title={t("schema.hiddenChanged")} /> : null}
      {source.status.state === "stale" && source.status.message && !dirty ? (
        <Alert tone="warn" title={t("prepare.staleHint")}>
          {source.status.message}
        </Alert>
      ) : null}
      {dirty ? (
        <div className="flex justify-end gap-2">
          <Button variant="ghost" disabled={update.isPending} onClick={() => setDraft(null)}>
            {tc("actions.cancel")}
          </Button>
          <Button
            variant="primary"
            loading={update.isPending}
            disabled={glossaryTooLong}
            onClick={save}
          >
            {t("schema.save")}
          </Button>
        </div>
      ) : null}
      <ExportSchemaDialog source={source} open={exporting} onOpenChange={setExporting} />
    </div>
  );
}
