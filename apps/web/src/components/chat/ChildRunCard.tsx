import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Download, Square } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { InteractionInput, RunEvent } from "@/api/run-events";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { JsonTree } from "@/components/common/JsonTree";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type { ChatSessionApi } from "@/features/sessions/chatSession";
import type { RunOut } from "@/features/sessions/types";
import { cn } from "@/lib/cn";
import { formatBytes, formatDuration, formatNumber, formatUsd } from "@/lib/format";
import { streamRunEvents } from "@/lib/sse";
import {
  applyRunEvents,
  initialRunState,
  isRunOpen,
  usageFromTotal,
  type ChildCallView,
  type PendingInteraction,
  type RunError,
  type RunStatus,
  type ToolCallView,
  type UsageTotals,
} from "@/stores/runStore";
import { RunStoreProvider, useRunStore } from "@/stores/runStoreContext";
import { ApprovalPrompt } from "./ApprovalPrompt";
import { ChildScopeContext, useChildScope, type ChildScope } from "./childScope";
import { ChildRunInspector, useChildSession, useNestedScope } from "./ChildRunInspector";
import { InteractionPrompt } from "./InteractionPrompt";
import { LiveElapsed } from "./RunStatusBar";
import { prettyJson } from "./toolSummary";

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

// 메시지 기록에서 다시 그린 카드는 같은 id가 겹치면 "@메시지 id"를 붙여 둔다(history.ts).
function callIdOf(tool: ToolCallView): string {
  return tool.id.split("@")[0];
}

/*
 * 메시지 기록에서 다시 그린 call_agent 카드는 어느 자식 런을 만들었는지 모른다. 그 부모 런의 저장된 이벤트를
 * 다시 받아 child_run.*로 채운다. 같은 런의 카드들이 한 번의 재생을 나눠 쓴다.
 */
async function replayChildCalls(
  api: ChatSessionApi,
  sessionId: string,
  runId: string,
  signal: AbortSignal,
): Promise<Record<string, ChildCallView>> {
  const events: RunEvent[] = [];
  await streamRunEvents({
    request: () => api.eventsRequest(sessionId, 0, runId),
    reconnect: () => null,
    onEvent: (event) => {
      if (event.type === "child_run.started" || event.type === "child_run.completed") {
        events.push(event);
      }
    },
    signal,
    fetch: api.fetch,
    maxRetries: 0,
  });
  const state = applyRunEvents(initialRunState({ runId, sessionId }), events);
  const calls: Record<string, ChildCallView> = {};
  for (const tool of Object.values(state.toolCalls)) {
    if (tool.child) calls[tool.id] = tool.child;
  }
  return calls;
}

function useRestoredChild(
  scope: ChildScope,
  tool: ToolCallView,
  parentSessionId: string | null,
): ChildCallView | null {
  const enabled = tool.child === null && tool.restored && Boolean(parentSessionId && tool.runId);
  const query = useQuery({
    queryKey: ["child-calls", parentSessionId, tool.runId],
    queryFn: ({ signal }) => replayChildCalls(scope.api, parentSessionId!, tool.runId, signal),
    enabled,
    staleTime: Infinity,
    retry: false,
  });
  return enabled ? (query.data?.[callIdOf(tool)] ?? null) : null;
}

interface CardStatus {
  tone: BadgeTone;
  label: string;
  spinner: boolean;
}

type EndStatus = "succeeded" | "failed" | "cancelled";

const END_TONE: Record<EndStatus, BadgeTone> = {
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
};

function isEnd(status: RunStatus): status is EndStatus {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

const RESULT_TONE: Record<string, BadgeTone> = {
  ok: "ok",
  error: "danger",
  timeout: "warn",
  cancelled: "neutral",
  rejected: "neutral",
};

interface ChildLiveView {
  runId: string | null;
  status: RunStatus;
  queuePosition: number | null;
  step: number;
  startedAt: string | null;
  pending: PendingInteraction | null;
}

function outputOf(run: RunOut | null): { result: unknown; files: OutputFile[] } {
  const output = run?.output ?? null;
  const files = Array.isArray(output?.files) ? (output.files as OutputFile[]) : [];
  return { result: output?.result ?? null, files };
}

// run.output.files 항목(서버 SessionFileRef)
interface OutputFile {
  id: string;
  path: string;
  size_bytes: number;
  downloadable: boolean;
}

// 끝난 자식 런의 결과 요약. 자식 세션의 마지막 런(run.output)에서 결과 파일과 JSON 결과를 읽는다.
function ChildResult({
  scope,
  sessionId,
  end,
  durationMs,
  conductorView,
}: {
  scope: ChildScope;
  sessionId: string;
  end: ChildCallView["end"];
  durationMs: number | null;
  conductorView: string;
}) {
  const { t } = useTranslation("chat");
  const detail = useQuery({
    queryKey: ["child-session", sessionId],
    queryFn: () => scope.api.getSession(sessionId),
    staleTime: Infinity,
  });
  const run = detail.data?.last_run ?? null;
  const { result, files } = outputOf(run);
  const usage: UsageTotals | null = run ? usageFromTotal(run.usage) : (end?.usage ?? null);
  const steps = run?.steps ?? end?.steps ?? null;
  const error: RunError | null = run?.error
    ? { code: run.error.code, message: run.error.message, retryable: run.error.retryable ?? false }
    : (end?.error ?? null);
  return (
    <section
      aria-label={t("child.result.label")}
      className="flex flex-col gap-2 rounded-md bg-sunken/70 px-2.5 py-2 text-xs"
    >
      <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-ink-muted">
        {steps !== null ? <span>{t("child.steps", { count: steps })}</span> : null}
        {usage ? (
          <span className="tabular-nums">
            {t("statusBar.tokens", {
              input: formatNumber(usage.inputTokens),
              output: formatNumber(usage.outputTokens),
            })}
            {usage.estimated ? ` (${t("statusBar.estimated")})` : ""}
          </span>
        ) : null}
        {usage?.costUsd !== null && usage?.costUsd !== undefined ? (
          <span className="tabular-nums">
            {t("statusBar.cost", { cost: formatUsd(usage.costUsd) })}
          </span>
        ) : null}
        {durationMs !== null ? <span>{formatDuration(durationMs)}</span> : null}
        {detail.isPending ? <Spinner className="size-3" label={t("child.loading")} /> : null}
      </p>
      {error ? (
        <p className="text-danger">
          <span className="font-mono">{error.code}</span> {error.message}
        </p>
      ) : null}
      {files.length > 0 ? (
        <ul aria-label={t("child.result.files")} className="flex flex-col gap-1">
          {files.map((file) => (
            <li key={file.id} className="flex items-center gap-2">
              {file.downloadable ? (
                <a
                  href={scope.fileUrl(sessionId, file.id)}
                  download
                  className="inline-flex min-w-0 items-center gap-1 text-accent hover:underline"
                  aria-label={t("files.download", { path: file.path })}
                >
                  <Download className="size-3.5 shrink-0" aria-hidden />
                  <span className="truncate font-mono">{file.path}</span>
                </a>
              ) : (
                <span className="min-w-0 truncate font-mono text-ink">{file.path}</span>
              )}
              <span className="shrink-0 text-ink-subtle tabular-nums">
                {formatBytes(file.size_bytes)}
              </span>
              {file.downloadable ? null : (
                <span className="shrink-0 text-ink-subtle">{t("files.notStored")}</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {result !== null && typeof result === "object" ? (
        <details open className="rounded-md bg-code px-2.5 py-1.5 font-mono text-code-ink">
          <summary className="cursor-pointer font-sans text-ink-muted select-none hover:text-ink">
            {t("child.result.json")}
          </summary>
          <div className="mt-1 max-h-72 overflow-auto">
            <JsonTree value={result} />
          </div>
        </details>
      ) : null}
      {detail.isError ? <p className="text-ink-subtle">{t("child.result.loadFailed")}</p> : null}
      {conductorView ? (
        <details>
          <summary className="cursor-pointer text-ink-muted select-none hover:text-ink">
            {t("child.result.toolResult")}
          </summary>
          <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-term px-2.5 py-2 font-mono whitespace-pre-wrap text-term-ink">
            {conductorView}
          </pre>
        </details>
      ) : null}
    </section>
  );
}

function ChildCard({
  tool,
  parentSessionId,
}: {
  tool: ToolCallView;
  parentSessionId: string | null;
}) {
  const { t } = useTranslation("chat");
  const scope = useChildScope();
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const restored = useRestoredChild(scope, tool, parentSessionId);
  const child = tool.child ?? restored;
  const input = tool.input ?? {};
  const slug = child?.agentSlug ?? text(input.agent);
  const name = child?.agentName ?? "";
  const who = name ? `${slug}(${name})` : slug;
  const task = child?.taskPreview ?? text(input.task);
  const passed = Array.isArray(input.files) ? input.files.length : 0;
  // 부모가 이 자식이 끝나기를 기다리는 동안은 접혀 있어도 자식 스트림을 받아 상태와 승인을 보여 준다.
  const waiting = tool.phase === "child" && !child?.end;
  const sessionId = child?.sessionId ?? null;
  const controller = useChildSession(scope, sessionId, waiting || open);
  const live: ChildLiveView = useStore(
    controller.store,
    useShallow((s) => ({
      runId: s.runId,
      status: s.status,
      queuePosition: s.queuePosition,
      step: s.step,
      startedAt: s.startedAt,
      pending: s.pendingInteraction,
    })),
  );
  const stopping = useStore(controller.meta, (meta) => meta.stopping);
  const nested = useNestedScope(scope, controller);
  // 자식이 다시 하위 에이전트를 부르고 기다리는 중이면 접힌 카드에도 그 손자 카드를 보인다. 손자의 승인 요청이
  // 거기 뜬다.
  const grandchildren = useStore(
    controller.store,
    useShallow((s) =>
      Object.values(s.toolCalls)
        .filter((item) => item.phase === "child" && item.child && !item.child.end)
        .map((item) => item.id),
    ),
  );
  const finished = tool.phase === "done" || Boolean(child?.end);

  let status: CardStatus;
  if (child?.end) {
    status = {
      tone: END_TONE[child.end.status],
      label: t(`child.end.${child.end.status}`),
      spinner: false,
    };
  } else if (tool.phase === "done") {
    status = {
      tone: RESULT_TONE[tool.result ?? "cancelled"] ?? "neutral",
      label: t(`tool.result.${tool.result ?? "cancelled"}`),
      spinner: false,
    };
  } else if (!waiting) {
    status = { tone: "info", label: t("child.status.calling"), spinner: true };
  } else if (live.status === "queued") {
    status = {
      tone: "info",
      label: live.queuePosition
        ? t("status.queued", { position: live.queuePosition })
        : t("status.queuedNoPosition"),
      spinner: true,
    };
  } else if (live.status === "waiting_input") {
    status = {
      tone: "warn",
      label:
        live.pending?.kind === "question" ? t("child.status.question") : t("child.status.approval"),
      spinner: false,
    };
  } else if (isEnd(live.status)) {
    status = { tone: END_TONE[live.status], label: t(`child.end.${live.status}`), spinner: false };
  } else {
    status = { tone: "info", label: t("status.running"), spinner: true };
  }

  const steps = child?.end?.steps ?? (waiting && live.step > 0 ? live.step : null);
  const durationMs = tool.durationMs ?? child?.end?.durationMs ?? null;
  const startedAt = waiting && live.startedAt ? new Date(live.startedAt).getTime() : null;
  const stoppable = waiting && live.runId !== null && isRunOpen(live.status);

  async function respond(interaction: PendingInteraction, value: InteractionInput) {
    setBusyId(interaction.interaction_id);
    try {
      const ok = await controller.respond(interaction.runId, value);
      if (ok && "decision" in value && value.decision === "approve" && value.session_auto_approve) {
        scope.markAutoApprove();
      }
    } finally {
      setBusyId(null);
    }
  }

  function renderInteraction(interaction: PendingInteraction) {
    const busy = busyId === interaction.interaction_id;
    if (interaction.kind === "approval") {
      return (
        <ApprovalPrompt
          interaction={interaction}
          busy={busy}
          title={t("child.approvalTitle", { agent: who })}
          autoApproveLabel={t("child.autoApprove")}
          autoApproveHint={t("child.autoApproveHint")}
          onApprove={({ sessionAutoApprove }) =>
            void respond(interaction, {
              decision: "approve",
              session_auto_approve: sessionAutoApprove,
            })
          }
          onReject={(note) => void respond(interaction, { decision: "reject", note: note || null })}
        />
      );
    }
    return (
      <InteractionPrompt
        interaction={interaction}
        busy={busy}
        onAnswer={(answer) => void respond(interaction, { answer })}
      />
    );
  }

  const conductorView = finished ? tool.output.map((chunk) => chunk.text).join("") : "";

  return (
    <Collapsible.Root
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "rounded-md border bg-surface",
        live.pending && waiting ? "border-warn/60" : "border-line",
      )}
    >
      <section aria-label={t("child.cardLabel", { agent: name || slug })}>
        <div className="flex items-center gap-2 px-3 py-2">
          <Collapsible.Trigger className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1 rounded-sm text-left">
            <ChevronRight
              className={cn(
                "size-3.5 shrink-0 text-ink-subtle transition-transform",
                open && "rotate-90",
              )}
              aria-hidden
            />
            <AgentIcon icon={null} name={name || slug} className="size-6 rounded-sm text-xs" />
            <span className="min-w-0 truncate text-sm font-medium text-ink">
              {name || slug || "…"}
            </span>
            {name ? (
              <span className="shrink-0 font-mono text-xs text-ink-subtle">{slug}</span>
            ) : null}
            {child?.version === "draft" ? (
              <Badge tone="warn" title={t("child.draftHint")}>
                {t("child.draft")}
              </Badge>
            ) : null}
            <Badge tone={status.tone}>
              {status.spinner ? <Spinner className="size-3" /> : null}
              {status.label}
            </Badge>
            <span className="inline-flex shrink-0 gap-2 text-xs text-ink-subtle tabular-nums">
              {steps !== null ? <span>{t("child.steps", { count: steps })}</span> : null}
              {startedAt !== null ? (
                <LiveElapsed start={startedAt} />
              ) : durationMs !== null ? (
                <span>{formatDuration(durationMs)}</span>
              ) : null}
            </span>
          </Collapsible.Trigger>
          {stoppable ? (
            <Button
              size="sm"
              variant="ghost"
              loading={stopping}
              aria-label={t("child.stopLabel", { agent: name || slug })}
              onClick={() => void controller.stop()}
            >
              <Square className="size-3.5" aria-hidden />
              {t("child.stop")}
            </Button>
          ) : null}
        </div>
        {open && !finished ? null : (
          <div className="flex flex-col gap-2 border-t border-line px-3 py-2.5">
            {open ? null : (
              <>
                {task ? (
                  <p className="line-clamp-2 text-xs whitespace-pre-wrap text-ink-muted">{task}</p>
                ) : null}
                {passed > 0 ? (
                  <p className="text-xs text-ink-subtle">
                    {t("child.filesPassed", { count: passed })}
                  </p>
                ) : null}
                {live.pending && waiting ? renderInteraction(live.pending) : null}
                {waiting && grandchildren.length > 0 ? (
                  <RunStoreProvider store={controller.store}>
                    <ChildScopeContext value={nested}>
                      {grandchildren.map((id) => (
                        <ChildRunCard key={id} toolCallId={id} />
                      ))}
                    </ChildScopeContext>
                  </RunStoreProvider>
                ) : null}
              </>
            )}
            {finished && sessionId ? (
              <ChildResult
                scope={scope}
                sessionId={sessionId}
                end={child?.end ?? null}
                durationMs={durationMs}
                conductorView={conductorView}
              />
            ) : null}
            {finished && !sessionId && conductorView ? (
              <pre className="max-h-60 overflow-auto rounded-md bg-term px-2.5 py-2 font-mono text-xs whitespace-pre-wrap text-term-ink">
                {conductorView}
              </pre>
            ) : null}
          </div>
        )}
        <Collapsible.Content>
          {sessionId ? (
            <ChildRunInspector
              controller={controller}
              scope={scope}
              renderInteraction={renderInteraction}
            />
          ) : (
            <pre className="border-t border-line px-3 py-3 font-mono text-xs whitespace-pre-wrap text-ink-subtle">
              {tool.input ? prettyJson(tool.input) : t("child.notStarted")}
            </pre>
          )}
        </Collapsible.Content>
      </section>
    </Collapsible.Root>
  );
}

// call_agent 도구 호출 카드(오케스트레이션 8.1). 부모 스트림의 child_run.*가 카드를 만들고 닫고, 카드 안은
// 자식 세션의 스트림이 채운다.
export function ChildRunCard({ toolCallId }: { toolCallId: string }) {
  const tool = useRunStore((state) => state.toolCalls[toolCallId]);
  const parentSessionId = useRunStore((state) => state.sessionId);
  if (!tool) return null;
  return <ChildCard tool={tool} parentSessionId={parentSessionId} />;
}
