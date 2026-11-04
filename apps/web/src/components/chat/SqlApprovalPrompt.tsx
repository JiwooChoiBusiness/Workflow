import { Check, DatabaseZap, Undo2, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { toast } from "@/components/ui/toast";
import {
  APPROVE,
  approvalSql,
  approvalSubject,
  approvalValues,
  REJECT,
  type SqlDecision,
} from "@/features/sql/approval";
import "@/features/sql/i18n";
import "@/features/workflows/i18n";
import { serverInputErrors } from "@/features/workflows/runInputs";
import { closedCode, type FormPromptProps } from "./FormPrompt";

/*
 * sql_query 노드의 승인 카드(3단계 설계 10.6, FormPrompt의 변형). 검증을 통과한 SQL을 편집기에 채워 보이고, 고쳐서
 * 승인하거나 거절한다. 답은 /input에 {kind: "form", values: {sql, decision}}이다. 고친 SQL은 서버가 실행 전에
 * 다시 검사하고, 거부되면 노드가 그 코드로 실패해 노드 카드·런 오류에 이유가 보인다(KS11).
 */
export function SqlApprovalPrompt({ form, nodeTitle, busy = false, onSubmit }: FormPromptProps) {
  const { t } = useTranslation("sql");
  const { t: tw } = useTranslation("workflows");
  const original = approvalSql(form);
  const subject = approvalSubject(form);
  const [sql, setSql] = useState(original);
  const [problem, setProblem] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [pending, setPending] = useState<SqlDecision | null>(null);
  const [sent, setSent] = useState<SqlDecision | null>(null);
  const locked = busy || pending !== null || sent !== null;
  const edited = sql.trim() !== original.trim();

  async function respond(decision: SqlDecision) {
    if (decision === APPROVE && !sql.trim()) {
      setProblem(t("approvalCard.empty"));
      return;
    }
    setProblem(null);
    setFailure(null);
    setPending(decision);
    let result: true | unknown;
    try {
      // 거절할 때도 sql 칸은 필수라 지금 SQL(비었으면 처음 SQL)을 같이 보낸다.
      result = await onSubmit(approvalValues(sql.trim() ? sql : original, decision));
    } finally {
      setPending(null);
    }
    if (result === true) {
      setSent(decision);
      return;
    }
    const closed = closedCode(result);
    if (closed) {
      toast.error(tw(`waitingForm.closed.${closed}`));
      setFailure(new Error(tw(`waitingForm.closed.${closed}`)));
      return;
    }
    if (isApiError(result, 422) && result.code === "invalid_inputs") {
      const byField = serverInputErrors(result.details);
      const message = byField.sql ?? byField.decision;
      if (message) {
        setProblem(message);
        return;
      }
    }
    setFailure(result);
  }

  return (
    <section
      aria-label={t("approvalCard.region")}
      className="flex flex-col gap-3 rounded-md border border-info/40 bg-info-soft/60 p-3"
    >
      <header className="flex items-start gap-2">
        <DatabaseZap className="mt-0.5 size-4 shrink-0 text-info" aria-hidden />
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-ink">
            {t("approvalCard.title", { node: nodeTitle })}
          </h3>
          {subject ? <p className="text-xs text-ink-muted">{subject}</p> : null}
        </div>
      </header>
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-ink-muted">{t("approvalCard.sql")}</span>
          {edited ? <Badge tone="warn">{t("approvalCard.edited")}</Badge> : null}
          {edited && !locked ? (
            <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setSql(original)}>
              <Undo2 className="size-3.5" aria-hidden />
              {t("approvalCard.reset")}
            </Button>
          ) : null}
        </div>
        <CodeEditor
          value={sql}
          onChange={setSql}
          language="text"
          readOnly={locked}
          ariaLabel={t("approvalCard.sql")}
          minHeight={96}
          maxHeight={280}
          invalid={problem !== null}
        />
        {problem ? (
          <p className="text-xs text-danger" role="alert">
            {problem}
          </p>
        ) : (
          <p className="text-xs text-ink-muted">{t("approvalCard.hint")}</p>
        )}
      </div>
      {failure ? <ErrorAlert error={failure} title={t("approvalCard.failed")} /> : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {sent ? (
          <p role="status" className="mr-auto text-xs text-ink-muted">
            {sent === APPROVE ? t("approvalCard.sentApprove") : t("approvalCard.sentReject")}
          </p>
        ) : null}
        <Button
          size="sm"
          variant="secondary"
          loading={pending === REJECT}
          disabled={locked}
          onClick={() => void respond(REJECT)}
        >
          <X className="size-3.5" aria-hidden />
          {t("approvalCard.reject")}
        </Button>
        <Button
          size="sm"
          variant="primary"
          loading={pending === APPROVE}
          disabled={locked}
          onClick={() => void respond(APPROVE)}
        >
          <Check className="size-3.5" aria-hidden />
          {t("approvalCard.approve")}
        </Button>
      </div>
    </section>
  );
}
