import { MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { WaitingInputData } from "@/api/run-events";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";

// ask_user 질문(설계 4.9). 옵션 버튼은 누르면 바로 답으로 보내고, 자유 입력은 따로 보낸다.
export function InteractionPrompt({
  interaction,
  allowFreeText = true,
  onAnswer,
  busy = false,
}: {
  interaction: WaitingInputData;
  allowFreeText?: boolean;
  onAnswer: (answer: string) => void;
  busy?: boolean;
}) {
  const { t } = useTranslation("chat");
  const [answer, setAnswer] = useState("");
  const titleId = useId();
  const answerId = useId();
  const options = interaction.options ?? [];
  const submit = () => {
    const value = answer.trim();
    if (value) onAnswer(value);
  };

  return (
    <section
      aria-labelledby={titleId}
      className="flex flex-col gap-3 rounded-md border border-info/40 bg-info-soft/60 p-3"
    >
      <header className="flex items-start gap-2">
        <MessageCircleQuestion className="mt-0.5 size-4 shrink-0 text-info" aria-hidden />
        <div>
          <h3 id={titleId} className="text-xs font-medium text-ink-muted">
            {t("question.title")}
          </h3>
          <p className="mt-0.5 text-sm whitespace-pre-wrap text-ink">{interaction.question}</p>
        </div>
      </header>
      {options.length > 0 ? (
        <div role="group" aria-label={t("question.options")} className="flex flex-wrap gap-2">
          {options.map((option) => (
            <Button key={option} size="sm" disabled={busy} onClick={() => onAnswer(option)}>
              {option}
            </Button>
          ))}
        </div>
      ) : null}
      {allowFreeText ? (
        <div className="flex flex-col gap-2">
          <label htmlFor={answerId} className="sr-only">
            {t("question.answer")}
          </label>
          <Textarea
            id={answerId}
            rows={2}
            className="min-h-0 bg-surface"
            value={answer}
            disabled={busy}
            placeholder={t("question.answerPlaceholder")}
            onChange={(event) => setAnswer(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
          />
          <div className="flex justify-end">
            <Button size="sm" variant="primary" disabled={busy || !answer.trim()} onClick={submit}>
              {t("question.send")}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
