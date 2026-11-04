import { CircleCheck, FileText } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { SqlExportSchemaOut, SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useKnowledgeBases } from "@/features/knowledge/api";
import { useExportSchema } from "@/features/sql/api";
import { useSqlError } from "@/features/sql/useSqlError";
import { failureText } from "@/features/sql/validation";

/*
 * 스키마를 지식 베이스로 내보내기(설계 10.7). 보이는 표마다 문서 하나를 넣고, 응답의 만든 문서·지운 이전 문서·
 * 실패한 표를 보인다. Q&A 지식 베이스는 질문·답 CSV만 받아 고를 수 없게 한다.
 */
export function ExportSchemaDialog({
  source,
  open,
  onOpenChange,
}: {
  source: SqlSourceOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const kbs = useKnowledgeBases();
  const exporter = useExportSchema(source.id);
  const errorText = useSqlError();
  const [kbId, setKbId] = useState("");
  const [result, setResult] = useState<SqlExportSchemaOut | null>(null);
  const choices = (kbs.data ?? []).filter((kb) => kb.doc_form !== "qa_model");
  const chosen = choices.find((kb) => kb.id === kbId);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setResult(null);
      exporter.reset();
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        title={t("export.title")}
        description={t("export.description")}
        className="w-[min(36rem,calc(100vw-2rem))]"
      >
        {result ? (
          <div className="flex flex-col gap-3">
            <Alert tone="info" title={t("export.result", { count: result.documents.length })}>
              {result.removed && result.removed.length > 0
                ? t("export.removed", { count: result.removed.length })
                : null}
            </Alert>
            <ul
              className="flex flex-col gap-1"
              aria-label={t("export.result", { count: result.documents.length })}
            >
              {result.documents.map((document) => (
                <li key={document.document_id} className="flex items-center gap-2 text-sm">
                  <CircleCheck className="size-3.5 shrink-0 text-ok" aria-hidden />
                  <FileText className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
                  <Link
                    to={`/knowledge/${result.kb_id}/documents/${document.document_id}`}
                    className="min-w-0 truncate text-accent hover:underline"
                  >
                    {document.name}
                  </Link>
                </li>
              ))}
            </ul>
            {result.failed && result.failed.length > 0 ? (
              <Alert tone="warn" title={t("export.failed", { count: result.failed.length })}>
                <ul className="flex flex-col gap-0.5">
                  {result.failed.map((failure) => {
                    const text = failureText(failure, t);
                    return (
                      <li key={failure.table}>
                        <span className="font-mono">{failure.table}</span>: {text.title}
                        {text.detail ? ` (${text.detail})` : null}
                      </li>
                    );
                  })}
                </ul>
              </Alert>
            ) : null}
            <p className="text-xs text-ink-muted">{t("export.renameNote")}</p>
            <DialogFooter>
              <Button variant="primary" onClick={() => close(false)}>
                {tc("actions.close")}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form
            className="flex flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (!chosen) return;
              exporter.mutate(chosen.id, { onSuccess: setResult });
            }}
          >
            {kbs.isPending ? (
              <Spinner className="size-4" label={tc("state.loading")} />
            ) : choices.length === 0 ? (
              <p className="text-sm text-ink-muted">
                {t("export.noKb")}{" "}
                <Link to="/knowledge" className="text-accent hover:underline">
                  {t("export.createKb")}
                </Link>
              </p>
            ) : (
              <Field label={t("export.kb")} required>
                <Select
                  value={kbId || undefined}
                  placeholder={t("export.pick")}
                  onValueChange={setKbId}
                  options={choices.map((kb) => ({ value: kb.id, label: kb.name }))}
                />
              </Field>
            )}
            {exporter.isError ? <Alert tone="danger" title={errorText(exporter.error)} /> : null}
            <DialogFooter>
              <Button variant="secondary" onClick={() => close(false)}>
                {tc("actions.cancel")}
              </Button>
              <Button
                type="submit"
                variant="primary"
                loading={exporter.isPending}
                disabled={!chosen}
              >
                {t("export.submit")}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
