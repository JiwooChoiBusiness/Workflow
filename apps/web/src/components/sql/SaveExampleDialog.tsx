import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useKnowledgeBases } from "@/features/knowledge/api";
import { useSaveSqlExample } from "@/features/sql/api";
import "@/features/sql/i18n";
import { useSqlError } from "@/features/sql/useSqlError";

const QUESTION_MAX = 1000;
const SQL_MAX = 8192;

/*
 * 예시로 저장(설계 10.7): (질문, SQL) 한 쌍을 Q&A 모드 지식 베이스의 문서 하나로 넣는다. 지식 검색이 질문끼리
 * 맞춰 SQL을 돌려주면 SQL을 만드는 프롬프트의 few-shot 예시가 된다. Q&A가 아니면 422 kb_not_qa, 같은 쌍은 409
 * duplicate_document다. SQL 시험과 노드 런 카드가 같이 쓴다.
 */
export function SaveExampleDialog({
  sourceId,
  sql,
  open,
  onOpenChange,
}: {
  sourceId: string;
  sql: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const kbs = useKnowledgeBases();
  const save = useSaveSqlExample(sourceId);
  const errorText = useSqlError();
  const [question, setQuestion] = useState("");
  const [text, setText] = useState<string | null>(null);
  const [kbId, setKbId] = useState("");
  const [problems, setProblems] = useState<{ question?: string; sql?: string; kb?: string }>({});
  const qaBases = (kbs.data ?? []).filter((kb) => kb.doc_form === "qa_model");
  const sqlText = text ?? sql;

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setQuestion("");
      setText(null);
      setProblems({});
      save.reset();
    }
  }

  function submit() {
    const found: typeof problems = {};
    if (!question.trim()) found.question = tc("validation.required");
    else if (question.trim().length > QUESTION_MAX) {
      found.question = tc("validation.maxLength", { max: QUESTION_MAX });
    }
    if (!sqlText.trim()) found.sql = tc("validation.required");
    else if (sqlText.trim().length > SQL_MAX)
      found.sql = tc("validation.maxLength", { max: SQL_MAX });
    if (!kbId) found.kb = tc("validation.required");
    setProblems(found);
    if (Object.keys(found).length > 0) return;
    const kb = qaBases.find((item) => item.id === kbId);
    save.mutate(
      { kb_id: kbId, question: question.trim(), sql: sqlText.trim() },
      {
        onSuccess: () => {
          toast.success(t("example.saved", { kb: kb?.name ?? "" }));
          close(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        title={t("example.title")}
        description={t("example.description")}
        className="w-[min(36rem,calc(100vw-2rem))]"
      >
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <Field label={t("example.question")} error={problems.question} required>
            <Input
              value={question}
              placeholder={t("example.questionPlaceholder")}
              onChange={(event) => setQuestion(event.target.value)}
            />
          </Field>
          <Field label={t("example.sql")} error={problems.sql} required>
            <Textarea
              rows={5}
              className="font-mono text-xs"
              value={sqlText}
              onChange={(event) => setText(event.target.value)}
            />
          </Field>
          {kbs.isPending ? (
            <Spinner className="size-4" label={tc("state.loading")} />
          ) : qaBases.length === 0 ? (
            <p className="text-sm text-ink-muted">
              {t("example.noKb")}{" "}
              <Link to="/knowledge" className="text-accent hover:underline">
                {t("example.createKb")}
              </Link>
            </p>
          ) : (
            <Field label={t("example.kb")} error={problems.kb} required>
              <Select
                value={kbId || undefined}
                placeholder={t("example.pick")}
                onValueChange={setKbId}
                options={qaBases.map((kb) => ({ value: kb.id, label: kb.name }))}
              />
            </Field>
          )}
          {save.isError ? <Alert tone="danger" title={errorText(save.error)} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => close(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              loading={save.isPending}
              disabled={qaBases.length === 0}
            >
              {t("example.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
