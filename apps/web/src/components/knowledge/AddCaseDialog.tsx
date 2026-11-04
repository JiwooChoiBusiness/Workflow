import { Search } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { RadioCards } from "@/components/ui/radio-cards";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useCreateEvalCase, useDocumentSearch } from "@/features/knowledge/api";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";

// 평가 질문 하나를 더한다. 정답 문서는 이름으로 찾아 고른다. 쓰는 쪽이 열 때마다 key를 바꿔 빈 상태로
// 시작하고, 질의 기록에서 열면 initialQuestion이 채워져 있다.
export function AddCaseDialog({
  kbId,
  open,
  onOpenChange,
  initialQuestion = "",
}: {
  kbId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialQuestion?: string;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const create = useCreateEvalCase(kbId);
  const [question, setQuestion] = useState(initialQuestion);
  const [keyword, setKeyword] = useState("");
  const [picked, setPicked] = useState<{ id: string; name: string } | null>(null);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const found = useDocumentSearch(kbId, keyword, open);
  const documents = found.data ?? [];

  let results;
  if (found.isPending) {
    results = <Spinner label={tc("state.loading")} className="text-ink-muted" />;
  } else if (found.isError) {
    results = <Alert tone="danger" title={errorText(found.error)} />;
  } else if (documents.length === 0) {
    results = <p className="text-sm text-ink-muted">{t("eval.documentSearchEmpty")}</p>;
  } else {
    results = (
      <div className="max-h-56 overflow-y-auto">
        <RadioCards
          ariaLabel={t("eval.expectedDocument")}
          value={picked?.id ?? ""}
          onValueChange={(value) => {
            const document = documents.find((item) => item.id === value);
            if (document) setPicked({ id: document.id, name: document.name });
          }}
          options={documents.map((item) => ({ value: item.id, label: item.name }))}
        />
      </div>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t("eval.addTitle")}>
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (!question.trim() || !picked) {
              setProblem(
                !question.trim() ? tc("validation.required") : t("errors.evalExpectedRequired"),
              );
              return;
            }
            create.mutate(
              {
                question: question.trim(),
                expected: { document_ids: [picked.id], segment_ids: [] },
                note: note.trim() || null,
              },
              {
                onSuccess: () => {
                  toast.success(t("eval.added"));
                  onOpenChange(false);
                },
                onError: (error) => setProblem(errorText(error)),
              },
            );
          }}
        >
          <Field label={t("eval.question")} required>
            <Textarea
              rows={2}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
          </Field>
          <div className="flex flex-col gap-2">
            <Field label={t("eval.expectedDocument")} hint={t("eval.expectedHint")} required>
              <div className="relative">
                <Search
                  className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-subtle"
                  aria-hidden
                />
                <Input
                  type="search"
                  value={keyword}
                  placeholder={t("eval.documentSearch")}
                  className="pl-8"
                  onChange={(event) => setKeyword(event.target.value)}
                />
              </div>
            </Field>
            {results}
            {picked ? (
              <p className="truncate text-xs text-ink-muted">
                {t("eval.picked", { name: picked.name })}
              </p>
            ) : null}
          </div>
          <Field label={t("eval.note")}>
            <Input value={note} onChange={(event) => setNote(event.target.value)} />
          </Field>
          {problem ? <Alert tone="danger" title={problem} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              {t("eval.add")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
