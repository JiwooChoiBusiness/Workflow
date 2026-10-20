import { ChevronRight, Lightbulb } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { isApiError } from "@/api/http";
import type { WfInteractionInput } from "@/api/run-events";
import type { FlowField, WfFileOut } from "@/api/types";
import { Composer } from "@/components/chat/Composer";
import { ConnectionNotice, MessageList } from "@/components/chat/MessageList";
import { RunStatusView } from "@/components/chat/RunStatusBar";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { ChatflowSession } from "@/features/workflows/chatflowSession";
import "@/features/workflows/i18n";
import {
  initialValues,
  serverInputErrors,
  toRunInputs,
  type FieldValue,
} from "@/features/workflows/runInputs";
import { finishedNodeCount, runningNode } from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import type { PendingInteraction } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { PendingPrompt } from "../run/PendingPrompt";
import { RunInputsForm } from "../run/RunInputsForm";

function LiveStatus({ session }: { session: ChatflowSession }) {
  const values = useStore(
    session.runs.store,
    useShallow((s) => ({
      status: s.status,
      step: finishedNodeCount(s),
      stepLimit: null,
      usage: s.usage,
      model: null,
      queuePosition: s.queuePosition,
      startedAt: s.startedAt,
      endedAt: s.endedAt,
      activity: s.status === "queued" || s.status === "running" ? runningNode(s) : null,
    })),
  );
  return <RunStatusView kind="workflow" values={values} />;
}

/*
 * 챗플로우 대화 하나(설계 7.3 ChatflowPreview, 7.4 웹앱 채팅). 1단계 채팅 목록(MessageList)과 입력창
 * (Composer)을 그대로 쓰고, 승인·질문 카드와 human_input 폼(목록 끝, 그동안 입력창 잠금)은 워크플로우 런의
 * /input으로 답한다. start에 입력 필드가 있으면 첫 메시지 전에 입력 폼을 보이고 그 값을 첫 메시지와 함께
 * 보낸다. 다음 메시지부터는 서버가 앞 값을 다시 쓴다.
 */
export function ChatflowChat({
  session,
  fields,
  upload,
  suggested = [],
  nodeTitle,
  beforeSend,
  onSendError,
  toolbar,
  className,
}: {
  session: ChatflowSession;
  fields: readonly FlowField[];
  // 입력 폼 파일 칸의 업로드
  upload: (file: File) => Promise<WfFileOut>;
  // 첫 메시지 전에 보일 추천 질문(settings.webapp.suggested_questions)
  suggested?: readonly string[];
  nodeTitle: (nodeId: string) => string;
  // 보내기 직전에 할 일(미리보기는 초안 저장). true가 아니면 보내지 않고 그 오류를 보인다.
  beforeSend?: () => Promise<true | unknown>;
  // 보내지 못한 이유를 쓰는 쪽이 처리했으면 true(미리보기의 검증 오류 등)
  onSendError?: (error: unknown) => boolean;
  // 상태 줄 오른쪽에 둘 것
  toolbar?: ReactNode;
  className?: string;
}) {
  const { t } = useTranslation("workflows");
  const meta = useStore(session.meta);
  // 목록(items)은 MessageList가 따로 읽는다. 여기서는 델타마다 다시 그리지 않게 나머지만 본다.
  const chat = useStore(
    session.chat,
    useShallow((s) => ({
      pending: s.pending,
      form: s.form,
      pendingNodeId: s.pendingNodeId,
      status: s.status,
      liveRunId: s.liveRunId,
      started: s.started,
    })),
  );
  const runMeta = useStore(session.runs.meta);
  const [values, setValues] = useState<Record<string, FieldValue>>(() => initialValues(fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  // 서버가 입력을 받지 않았으면 대화를 시작한 뒤에도 폼을 다시 연다.
  const [inputsOpen, setInputsOpen] = useState(false);
  const [formOpen, setFormOpen] = useState(true);
  const [sendError, setSendError] = useState<unknown>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // 다른 대화로 가면 앞 대화의 칸 오류·안내를 지운다(입력값은 다음 대화에 그대로 둔다).
  const [shown, setShown] = useState(meta.conversationId);
  if (shown !== meta.conversationId) {
    setShown(meta.conversationId);
    setErrors({});
    setInputsOpen(false);
    setFormOpen(true);
    setSendError(null);
  }
  const pending = chat.pending;
  const answerMode = pending?.kind === "question";
  const allowFreeText = pending?.kind !== "question" || pending.allow_free_text !== false;
  const showInputs = fields.length > 0 && (!chat.started || inputsOpen);
  const live = chat.liveRunId !== null;

  async function send(text: string, files: File[]): Promise<boolean> {
    let inputs: Record<string, unknown> = {};
    if (showInputs) {
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
        setInputsOpen(true);
        setFormOpen(true);
        return false;
      }
      inputs = parsed.inputs;
    }
    setErrors({});
    setSendError(null);
    if (beforeSend) {
      const ready = await beforeSend();
      if (ready !== true) {
        setSendError(ready);
        return false;
      }
    }
    const result = await session.send(text, files, inputs);
    if (result === true) {
      setInputsOpen(false);
      return true;
    }
    if (result === false) return false;
    if (isApiError(result, 422) && result.code === "invalid_inputs") {
      setErrors(serverInputErrors(result.details));
      setInputsOpen(true);
      setFormOpen(true);
      return false;
    }
    if (!onSendError?.(result)) setSendError(result);
    return false;
  }

  async function respond(interaction: PendingInteraction, input: WfInteractionInput) {
    setBusyId(interaction.interaction_id);
    try {
      return await session.respond(input);
    } finally {
      setBusyId(null);
    }
  }

  function renderInteraction(interaction: PendingInteraction) {
    const nodeId = chat.pendingNodeId ?? "";
    return (
      <PendingPrompt
        pending={{
          nodeId,
          iterationIndex: null,
          childRunId: null,
          kind: interaction.kind,
          interaction,
          form: null,
        }}
        nodeTitle={nodeTitle(nodeId)}
        busy={busyId === interaction.interaction_id || runMeta.responding}
        onRespond={(input) => void respond(interaction, input)}
      />
    );
  }

  return (
    <div className={cn("relative flex h-full min-h-0 flex-col bg-canvas", className)}>
      <div className="flex min-h-10 items-center gap-2 border-b border-line bg-surface px-4 py-1.5">
        <div className="min-w-0 flex-1">{live ? <LiveStatus session={session} /> : null}</div>
        {toolbar}
      </div>
      {live && runMeta.connection ? (
        <ConnectionNotice status={runMeta.connection.status} attempt={runMeta.connection.attempt} />
      ) : null}
      {meta.loadError ? (
        <ErrorAlert
          error={meta.loadError}
          title={t("chat.loadFailed")}
          onRetry={() => void session.resync()}
          className="mx-4 mt-3"
        />
      ) : null}
      {showInputs ? (
        <Collapsible.Root open={formOpen} onOpenChange={setFormOpen} asChild>
          <section
            aria-label={t("chat.inputs")}
            className="max-h-[45%] shrink-0 overflow-y-auto border-b border-line bg-surface px-4 py-2.5"
          >
            <Collapsible.Trigger className="flex w-full items-center gap-1.5 text-left text-sm font-semibold text-ink">
              <ChevronRight
                className={cn(
                  "size-3.5 text-ink-subtle transition-transform",
                  formOpen && "rotate-90",
                )}
                aria-hidden
              />
              {t("chat.inputs")}
            </Collapsible.Trigger>
            <Collapsible.Content className="pt-2">
              <p className="mb-3 text-xs text-ink-muted">{t("chat.inputsHint")}</p>
              <RunInputsForm
                fields={fields}
                values={values}
                onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
                errors={errors}
                upload={upload}
                disabled={meta.sending}
              />
            </Collapsible.Content>
          </section>
        </Collapsible.Root>
      ) : null}
      {meta.loading ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-sm text-ink-muted">
          <Spinner className="size-4" />
          {t("chat.loading")}
        </div>
      ) : (
        <RunStoreProvider store={session.view}>
          <MessageList
            renderInteraction={renderInteraction}
            fileHref={session.fileHref}
            footer={
              chat.form ? (
                <PendingPrompt
                  pending={chat.form}
                  nodeTitle={nodeTitle(chat.form.nodeId)}
                  busy={runMeta.responding}
                  onRespond={(input) => void session.respond(input)}
                  upload={upload}
                  onSubmitForm={(values) => session.submitForm(values)}
                />
              ) : null
            }
          />
        </RunStoreProvider>
      )}
      {sendError ? (
        <ErrorAlert error={sendError} title={t("chat.sendFailed")} className="mx-4 mb-2" />
      ) : null}
      {!chat.started && suggested.length > 0 && !meta.loading ? (
        <div
          role="group"
          aria-label={t("chat.suggested")}
          className="flex flex-wrap gap-1.5 border-t border-line bg-surface px-4 py-2.5"
        >
          {suggested.map((question) => (
            <Button
              key={question}
              size="sm"
              disabled={meta.sending}
              onClick={() => void send(question, [])}
            >
              <Lightbulb className="size-3.5" aria-hidden />
              {question}
            </Button>
          ))}
        </div>
      ) : null}
      <Composer
        running={chat.status === "queued" || chat.status === "running"}
        stoppable={chat.status === "waiting_input"}
        stopping={runMeta.stopping}
        disabled={
          meta.loading ||
          meta.sending ||
          runMeta.starting ||
          pending?.kind === "approval" ||
          chat.form !== null ||
          (answerMode && !allowFreeText) ||
          busyId !== null
        }
        answerMode={answerMode}
        attachments
        onStop={() => void session.stop()}
        onSend={(text, files) =>
          answerMode && pending ? respond(pending, { answer: text }) : send(text, files)
        }
      />
    </div>
  );
}
