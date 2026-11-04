import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/input";

// 청크·자식 청크를 더하거나 고치는 대화상자. Q&A는 질문과 답 두 칸이다. 저장은 부르는 쪽 변이(onSave)가
// 하고, 실패하면 그 문장을 보인다. 대상마다 key를 바꿔 다시 만든다.
export function SegmentEditor({
  open,
  onOpenChange,
  title,
  hint,
  initialContent = "",
  initialAnswer,
  qa,
  pending,
  error,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  hint?: string;
  initialContent?: string;
  initialAnswer?: string | null;
  qa?: boolean;
  pending?: boolean;
  error?: string | null;
  onSave: (content: string, answer: string | null) => void;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const [content, setContent] = useState(initialContent);
  const [answer, setAnswer] = useState(initialAnswer ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title} description={hint} className="w-[min(44rem,calc(100vw-2rem))]">
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (!content.trim() || (qa && !answer.trim())) {
              setProblem(tc("validation.required"));
              return;
            }
            setProblem(null);
            onSave(content, qa ? answer : null);
          }}
        >
          <Field
            label={qa ? t("segments.question") : t("segments.content")}
            hint={t("segments.chars", { count: content.length })}
            required
          >
            <Textarea
              rows={qa ? 3 : 10}
              value={content}
              onChange={(event) => setContent(event.target.value)}
            />
          </Field>
          {qa ? (
            <Field label={t("segments.answer")} required>
              <Textarea
                rows={6}
                value={answer}
                onChange={(event) => setAnswer(event.target.value)}
              />
            </Field>
          ) : null}
          {problem ? <Alert tone="danger" title={problem} /> : null}
          {error ? <Alert tone="danger" title={error} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button type="submit" variant="primary" loading={pending}>
              {tc("actions.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
