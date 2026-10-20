import { ClipboardList } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import type { WorkflowFormData } from "@/api/run-events";
import type { WfFileOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { RunInputsForm } from "@/components/flow/run/RunInputsForm";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import "@/features/workflows/i18n";
import {
  formFieldsOf,
  initialValues,
  serverInputErrors,
  toRunInputs,
  type FieldValue,
} from "@/features/workflows/runInputs";
import { AssistantMessage } from "./AssistantMessage";

// 409로 돌아온 경우. 런이 이미 이어졌거나 끝나서 카드가 곧 사라지므로 알림으로도 남긴다.
const CLOSED_CODES = ["input_timeout", "run_not_waiting", "wrong_input_kind"] as const;
type ClosedCode = (typeof CLOSED_CODES)[number];

function closedCode(error: unknown): ClosedCode | null {
  if (!isApiError(error, 409)) return null;
  return (CLOSED_CODES as readonly string[]).includes(error.code)
    ? (error.code as ClosedCode)
    : null;
}

/*
 * human_input 노드가 기다리는 폼(2단계 설계 2.4, M19). 안내문(마크다운)과 필드를 그리고, 보내면 같은 런이
 * 이어서 돈다. 필드는 런 입력 폼(start 필드)과 같은 모양이라 RunInputsForm을 그대로 쓰고, 파일 칸은 고르는
 * 즉시 올려 받은 id를 값으로 보낸다. onSubmit은 서버가 받았으면 true, 아니면 그 오류를 돌려준다.
 */
export function FormPrompt({
  form,
  nodeTitle,
  upload,
  busy = false,
  onSubmit,
}: {
  form: WorkflowFormData;
  nodeTitle: string;
  upload: (file: File) => Promise<WfFileOut>;
  busy?: boolean;
  onSubmit: (values: Record<string, unknown>) => Promise<true | unknown>;
}) {
  const { t } = useTranslation("workflows");
  const titleId = useId();
  const fields = useMemo(() => formFieldsOf(form.fields), [form.fields]);
  const [values, setValues] = useState<Record<string, FieldValue>>(() => initialValues(fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const locked = busy || submitting || sent;

  async function submit() {
    const parsed = toRunInputs(fields, values);
    const names = Object.keys(parsed.problems);
    if (names.length > 0) {
      setErrors(
        Object.fromEntries(
          names.map((name) => [
            name,
            parsed.problems[name] === "number" ? t("run.invalidNumber") : t("run.required"),
          ]),
        ),
      );
      return;
    }
    setErrors({});
    setFailure(null);
    setSubmitting(true);
    let result: true | unknown;
    try {
      result = await onSubmit(parsed.inputs);
    } finally {
      setSubmitting(false);
    }
    if (result === true) {
      setSent(true);
      return;
    }
    const closed = closedCode(result);
    if (closed) {
      toast.error(t(`waitingForm.closed.${closed}`));
      setFailure(new Error(t(`waitingForm.closed.${closed}`)));
      return;
    }
    if (isApiError(result, 422) && result.code === "invalid_inputs") {
      const byField = serverInputErrors(result.details);
      setErrors(byField);
      // 칸에 붙지 않는 사유(모르는 필드 등)는 카드 아래에 보인다.
      if (!Object.keys(byField).some((name) => fields.some((field) => field.name === name))) {
        setFailure(result);
      }
      return;
    }
    setFailure(result);
  }

  return (
    <section
      aria-labelledby={titleId}
      className="flex flex-col gap-3 rounded-md border border-info/40 bg-info-soft/60 p-3"
    >
      <header className="flex items-start gap-2">
        <ClipboardList className="mt-0.5 size-4 shrink-0 text-info" aria-hidden />
        <h3 id={titleId} className="text-sm font-medium text-ink">
          {t("waitingForm.title", { node: nodeTitle })}
        </h3>
      </header>
      {form.description ? (
        <div className="rounded-md bg-surface px-3 py-2">
          <AssistantMessage text={form.description} streaming={false} />
        </div>
      ) : null}
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (!locked) void submit();
        }}
      >
        <RunInputsForm
          fields={fields}
          values={values}
          onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
          errors={errors}
          upload={upload}
          disabled={locked}
        />
        {failure ? <ErrorAlert error={failure} title={t("waitingForm.failed")} /> : null}
        <div className="flex items-center justify-end gap-2">
          {sent ? (
            <p role="status" className="mr-auto text-xs text-ink-muted">
              {t("waitingForm.sent")}
            </p>
          ) : null}
          <Button type="submit" size="sm" variant="primary" loading={submitting} disabled={locked}>
            {t("waitingForm.submit")}
          </Button>
        </div>
      </form>
    </section>
  );
}
