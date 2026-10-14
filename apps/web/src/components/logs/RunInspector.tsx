import {
  Bot,
  Check,
  ChevronRight,
  CircleAlert,
  CornerDownRight,
  Download,
  FileDiff,
  Info,
  MessageSquareReply,
  X,
} from "lucide-react";
import { Collapsible } from "radix-ui";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { ToolIcon } from "@/components/chat/ToolIcon";
import { prettyJson, summarizeInput } from "@/components/chat/toolSummary";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { KeyValueList } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  sessionFileUrl,
  useChildRuns,
  useRun,
  useRunEvents,
  useTreeUsage,
  type TreeUsage,
} from "@/features/logs/api";
import { sessionLogPath } from "@/features/logs/filters";
import {
  inspectRun,
  type InspectorInteraction,
  type InspectorNotice,
  type InspectorStep,
  type InspectorToolCall,
} from "@/features/logs/inspector";
import { isRunActive, type RunOut } from "@/features/logs/types";
import { formatCost } from "@/features/settings/usage";
import { cn } from "@/lib/cn";
import { formatBytes, formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import { RunStatusBadge } from "./badges";
import { ChildRunList } from "./ChildRunList";
import { EventTable } from "./EventTable";
import { ToolOutputActions } from "./ToolOutputDialog";

const RESULT_TONE: Record<string, BadgeTone> = {
  ok: "ok",
  error: "danger",
  timeout: "warn",
  cancelled: "neutral",
  rejected: "neutral",
};

const TRIGGER_KEYS = {
  chat: "trigger.chat",
  task: "trigger.task",
  resume: "trigger.resume",
} as const;
const APPROVAL_KEYS = {
  off: "approvalMode.off",
  http_writes: "approvalMode.http_writes",
  all_shell: "approvalMode.all_shell",
} as const;
const FILE_REASON_KEYS = {
  too_large: "fileReason.too_large",
  run_limit: "fileReason.run_limit",
  copy_failed: "fileReason.copy_failed",
} as const;

function keyOf<K extends string, V>(table: Record<K, V>, value: string | null): V | null {
  return value !== null && value in table ? table[value as K] : null;
}

function duration(run: RunOut): number | null {
  if (!run.started_at) return null;
  const end = run.ended_at ? new Date(run.ended_at).getTime() : null;
  return end === null ? null : end - new Date(run.started_at).getTime();
}

// 이 런과 하위 런들의 합. 가격표가 없는 런이 하나라도 있으면 비용은 빼고 보인다.
function TreeTotal({ run, tree }: { run: RunOut; tree: TreeUsage }) {
  const { t } = useTranslation("logs");
  const own = run.usage.cost_usd;
  const values = {
    input: formatNumber((run.usage.input_tokens ?? 0) + tree.inputTokens),
    output: formatNumber((run.usage.output_tokens ?? 0) + tree.outputTokens),
    runs: tree.runs,
  };
  const cost =
    own === null || own === undefined || tree.costUsd === null ? null : own + tree.costUsd;
  return (
    <span className="tabular-nums">
      {cost === null
        ? t("inspector.treeTotalNoCost", values)
        : t("inspector.treeTotalValue", { ...values, cost: formatCost(cost) })}
    </span>
  );
}

function RunSummary({ run, tree }: { run: RunOut; tree: TreeUsage | undefined }) {
  const { t } = useTranslation("logs");
  const model = run.model as { model_id?: string | null; used?: string; fallback_used?: boolean };
  const usage = run.usage;
  const trigger = keyOf(TRIGGER_KEYS, run.trigger);
  const approval = keyOf(APPROVAL_KEYS, run.approval_mode);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <RunStatusBadge status={run.status} />
        <Badge>{trigger ? t(trigger) : run.trigger}</Badge>
        <Badge tone={run.approval_mode === "off" ? "danger" : "neutral"}>
          {approval ? t(approval) : run.approval_mode}
        </Badge>
        <span className="font-mono text-xs text-ink-subtle">{run.id}</span>
      </div>
      {run.error ? (
        <Alert tone="danger" title={t("inspector.error", { code: run.error.code })}>
          {run.error.message}
          {run.error.retryable ? (
            <span className="block text-xs">{t("inspector.retryable")}</span>
          ) : null}
        </Alert>
      ) : null}
      <KeyValueList
        className="grid-cols-[max-content_1fr] text-xs sm:grid-cols-[max-content_1fr_max-content_1fr]"
        items={[
          {
            label: t("inspector.model"),
            value: (
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono">
                  {model.used ?? model.model_id ?? t("inspector.defaultModel")}
                </span>
                {model.fallback_used ? <Badge tone="warn">{t("inspector.fallback")}</Badge> : null}
              </span>
            ),
          },
          { label: t("inspector.steps"), value: formatNumber(run.steps) },
          {
            label: t("inspector.tokens"),
            value: (
              <span className="tabular-nums">
                {t("tokensPair", {
                  input: formatNumber(usage.input_tokens),
                  output: formatNumber(usage.output_tokens),
                })}
                {usage.estimated ? ` (${t("estimated")})` : ""}
              </span>
            ),
          },
          {
            label: t("inspector.reasoningCached"),
            value: (
              <span className="tabular-nums">
                {formatNumber(usage.reasoning_tokens ?? 0)} /{" "}
                {formatNumber(usage.cached_input_tokens ?? 0)}
              </span>
            ),
          },
          { label: t("inspector.cost"), value: formatCost(usage.cost_usd) },
          ...(tree && tree.runs > 0
            ? [{ label: t("inspector.treeTotal"), value: <TreeTotal run={run} tree={tree} /> }]
            : []),
          { label: t("inspector.duration"), value: formatDuration(duration(run)) },
          { label: t("inspector.queued"), value: formatDateTime(run.queued_at) },
          { label: t("inspector.ended"), value: formatDateTime(run.ended_at) },
        ]}
      />
    </div>
  );
}

function InteractionLine({ interaction }: { interaction: InspectorInteraction }) {
  const { t } = useTranslation("logs");
  const resolution = interaction.resolution;
  let icon: ReactNode = <MessageSquareReply className="size-3.5 text-ink-muted" aria-hidden />;
  let label: string;
  if (!resolution) {
    label =
      interaction.kind === "approval"
        ? t("interaction.waitingApproval")
        : t("interaction.waitingAnswer");
  } else if (resolution.decision === "approve") {
    icon = <Check className="size-3.5 text-ok" aria-hidden />;
    label = resolution.autoApprove ? t("interaction.approvedAuto") : t("interaction.approved");
  } else if (resolution.decision === "reject") {
    icon = <X className="size-3.5 text-danger" aria-hidden />;
    label = resolution.note
      ? t("interaction.rejectedNote", { note: resolution.note })
      : t("interaction.rejected");
  } else {
    label = t("interaction.answered", { answer: resolution.answer ?? "" });
  }
  return (
    <p className="flex items-start gap-1.5 text-xs text-ink">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <span className="whitespace-pre-wrap">
        {interaction.question ? (
          <span className="text-ink-muted">
            {t("interaction.question", { question: interaction.question })}{" "}
          </span>
        ) : null}
        {label}
        {resolution ? (
          <span className="ml-1.5 text-ink-subtle">{formatDateTime(resolution.resolvedAt)}</span>
        ) : null}
      </span>
    </p>
  );
}

function ToolCallDetail({
  runId,
  tool,
  focused,
}: {
  runId: string;
  tool: InspectorToolCall;
  focused: boolean;
}) {
  const { t } = useTranslation("logs");
  const [open, setOpen] = useState(focused);
  const truncated = tool.outputBytes !== null && tool.outputBytes > tool.outputExcerpt.length;
  return (
    <Collapsible.Root
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "rounded-md border bg-surface",
        focused ? "border-accent ring-2 ring-accent/30" : "border-line",
      )}
    >
      <div
        ref={(node) => {
          if (node && focused) node.scrollIntoView({ block: "center" });
        }}
      >
        <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-hover">
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 text-ink-subtle transition-transform",
              open && "rotate-90",
            )}
            aria-hidden
          />
          <ToolIcon name={tool.name} className="size-4 shrink-0 text-ink-muted" />
          <span className="shrink-0 font-mono text-xs font-medium text-ink">
            {tool.name || "…"}
          </span>
          <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">
            {summarizeInput(tool.name, tool.input, "")}
          </span>
          {tool.interaction?.kind === "approval" ? (
            <Badge
              tone={
                tool.interaction.resolution?.decision === "approve"
                  ? "ok"
                  : tool.interaction.resolution?.decision === "reject"
                    ? "danger"
                    : "warn"
              }
            >
              {tool.interaction.resolution?.decision === "approve"
                ? t("tool.approved")
                : tool.interaction.resolution?.decision === "reject"
                  ? t("tool.rejected")
                  : t("tool.awaitingApproval")}
            </Badge>
          ) : null}
          {tool.status ? (
            <Badge tone={RESULT_TONE[tool.status] ?? "neutral"}>
              {t(`toolStatus.${tool.status}`)}
            </Badge>
          ) : (
            <Badge tone="info">{t("tool.pending")}</Badge>
          )}
          {tool.durationMs !== null ? (
            <span className="shrink-0 text-xs text-ink-subtle tabular-nums">
              {formatDuration(tool.durationMs)}
            </span>
          ) : null}
        </Collapsible.Trigger>
      </div>
      <Collapsible.Content className="flex flex-col gap-3 border-t border-line px-3 py-3">
        {tool.child ? (
          <p className="flex flex-wrap items-center gap-2 text-xs text-ink">
            <Bot className="size-3.5 text-ink-muted" aria-hidden />
            <span>
              {tool.child.agentName || tool.child.agentSlug}{" "}
              <span className="font-mono text-ink-subtle">{tool.child.agentSlug}</span>
            </span>
            {tool.child.version === "draft" ? (
              <Badge tone="warn">{t("children.draft")}</Badge>
            ) : null}
            {tool.child.status ? <RunStatusBadge status={tool.child.status} /> : null}
            <Link
              to={sessionLogPath(tool.child.sessionId, tool.child.runId)}
              className="text-accent hover:underline"
            >
              {t("inspector.openChild")}
            </Link>
          </p>
        ) : null}
        {tool.interaction ? <InteractionLine interaction={tool.interaction} /> : null}
        <section>
          <h4 className="mb-1 text-xs font-medium text-ink-muted">{t("tool.input")}</h4>
          <pre className="max-h-64 overflow-auto rounded-md bg-code px-3 py-2 font-mono text-xs whitespace-pre-wrap text-code-ink">
            {tool.input ? prettyJson(tool.input) : t("tool.noInput")}
          </pre>
        </section>
        {tool.status ? (
          <section>
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-xs font-medium text-ink-muted">
                {truncated ? t("tool.excerpt") : t("tool.output")}
              </h4>
              <ToolOutputActions
                runId={runId}
                toolCallId={tool.id}
                name={tool.name}
                outputBytes={tool.outputBytes}
              />
            </div>
            <pre className="max-h-72 overflow-auto rounded-md bg-term px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-term-ink">
              {tool.outputExcerpt || t("tool.noOutput")}
            </pre>
            <footer className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
              {tool.exitCode !== null ? (
                <span className={tool.exitCode === 0 ? undefined : "text-danger"}>
                  {t("tool.exitCode", { code: tool.exitCode })}
                </span>
              ) : null}
              {tool.timedOut ? <span className="text-warn">{t("tool.timedOut")}</span> : null}
              {tool.durationMs !== null ? <span>{formatDuration(tool.durationMs)}</span> : null}
              {tool.peakMemoryBytes ? (
                <span title={t("tool.peakMemoryHint")}>
                  {t("tool.peakMemory", { size: formatBytes(tool.peakMemoryBytes) })}
                </span>
              ) : null}
              {tool.outputBytes !== null ? (
                <span>{t("tool.outputSize", { size: formatBytes(tool.outputBytes) })}</span>
              ) : null}
            </footer>
          </section>
        ) : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

function NoticeLine({ notice }: { notice: InspectorNotice }) {
  const { t } = useTranslation("logs");
  const data = notice.data;
  const isError = notice.type === "run.error";
  let text: string;
  switch (notice.type) {
    case "run.error":
      text = t("notice.error", {
        code: String(data.code ?? ""),
        message: String(data.message ?? ""),
      });
      break;
    case "run.retrying":
      text = t("notice.retrying", {
        attempt: Number(data.attempt ?? 1),
        reason: String(data.reason ?? ""),
      });
      break;
    case "run.resumed":
      text = t("notice.resumed");
      break;
    case "context.compacted":
      text = t("notice.compacted", {
        before: formatNumber(Number(data.before_tokens ?? 0)),
        after: formatNumber(Number(data.after_tokens ?? 0)),
      });
      break;
    case "context.prompt_changed":
      text = t("notice.promptChanged");
      break;
    case "workspace.restoring":
      text = t("notice.workspaceRestoring");
      break;
    case "workspace.restored":
      text = t("notice.workspaceRestored", { packages: Number(data.packages ?? 0) });
      break;
    case "workspace.restore_warning":
      text = t("notice.workspaceWarning", { message: String(data.message ?? "") });
      break;
    case "workspace.recreated":
      text = t("notice.workspaceRecreated");
      break;
    default:
      text = notice.type;
  }
  const Icon = isError ? CircleAlert : Info;
  return (
    <p
      className={cn(
        "flex items-start gap-1.5 text-xs",
        isError ? "text-danger" : "text-ink-subtle",
      )}
    >
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span className="whitespace-pre-wrap">{text}</span>
    </p>
  );
}

function StepCard({
  runId,
  step,
  focusToolCallId,
}: {
  runId: string;
  step: InspectorStep;
  focusToolCallId: string | null;
}) {
  const { t } = useTranslation("logs");
  return (
    <li className="flex flex-col gap-2">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="font-semibold text-ink">
          {step.step > 0 ? t("inspector.step", { step: step.step }) : t("inspector.unknownStep")}
        </span>
        {step.usage ? (
          <span className="text-ink-muted tabular-nums">
            {t("tokensPair", {
              input: formatNumber(step.usage.inputTokens),
              output: formatNumber(step.usage.outputTokens),
            })}
            {step.usage.estimated ? ` (${t("estimated")})` : ""}
            {step.usage.costUsd !== null ? ` · ${formatCost(step.usage.costUsd)}` : ""}
            {" · "}
            <span className="font-mono">{step.usage.model}</span>
            {step.usage.keyIndex !== null
              ? ` · ${t("inspector.keyIndex", { index: step.usage.keyIndex })}`
              : ""}
          </span>
        ) : null}
      </header>
      {step.notices.map((notice) => (
        <NoticeLine key={notice.seq} notice={notice} />
      ))}
      {step.toolCalls.map((tool) => (
        <ToolCallDetail
          // 인스펙터로 보낸 도구 호출이 바뀌면 다시 만들어 펼친다.
          key={`${tool.id}:${tool.id === focusToolCallId ? "focus" : ""}`}
          runId={runId}
          tool={tool}
          focused={tool.id === focusToolCallId}
        />
      ))}
      {step.messages.map((message) =>
        message.text ? (
          <blockquote
            key={message.id}
            className="border-l-2 border-line pl-3 text-xs whitespace-pre-wrap text-ink-muted"
          >
            {message.text.length > 600 ? `${message.text.slice(0, 600)}…` : message.text}
            {message.stopReason !== "end_turn" && message.stopReason !== "tool_use" ? (
              <Badge className="ml-2">
                {t("timeline.stopReason", { reason: message.stopReason })}
              </Badge>
            ) : null}
          </blockquote>
        ) : null,
      )}
    </li>
  );
}

function ParentCrumb({ parentRunId, depth }: { parentRunId: string; depth: number }) {
  const { t } = useTranslation("logs");
  const parent = useRun(parentRunId);
  const label = <span className="font-mono">{parentRunId.slice(0, 8)}</span>;
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
      <CornerDownRight className="size-3.5" aria-hidden />
      {t("inspector.parentRun")}
      {parent.data ? (
        <Link
          to={sessionLogPath(parent.data.session_id, parentRunId)}
          className="text-accent hover:underline"
        >
          {label}
        </Link>
      ) : (
        label
      )}
      <span>· {t("inspector.depth", { depth })}</span>
    </p>
  );
}

// 런 인스펙터(설계 7.3 RunInspector): 스텝 타임라인, 도구 호출 입출력, 승인 결정과 사유, 사용량.
// call_agent로 부른 자식 런은 하위 런 탭에서 같은 인스펙터로 펼친다(오케스트레이션 8.3). nested는 그 안쪽이다.
// kind=node_child는 워크플로우 agent 노드가 만든 자식 런(2단계 A10)이다. 세션은 런에서 읽고, 그 과업
// 세션의 로그로 가는 링크를 둔다.
export function RunInspector({
  runId,
  sessionId: givenSessionId,
  focusToolCallId = null,
  initialTab = "steps",
  nested = false,
  kind = "session",
}: {
  runId: string;
  sessionId?: string;
  focusToolCallId?: string | null;
  initialTab?: "steps" | "children";
  nested?: boolean;
  kind?: "session" | "node_child";
}) {
  const { t } = useTranslation("logs");
  const run = useRun(runId);
  const live = isRunActive(run.data?.status);
  const events = useRunEvents(runId, { live });
  const children = useChildRuns(runId, { live });
  const childRuns = children.data ?? [];
  const tree = useTreeUsage(runId, { enabled: childRuns.length > 0, live });
  if (run.isPending || events.isPending) {
    return (
      <div className="flex justify-center p-6">
        <Spinner label={t("inspector.loading")} />
      </div>
    );
  }
  if (run.isError || events.isError) {
    return (
      <ErrorAlert
        error={run.error ?? events.error}
        title={t("inspector.loadFailed")}
        onRetry={() => {
          void run.refetch();
          void events.refetch();
        }}
      />
    );
  }
  const inspection = inspectRun(events.data.items);
  const interactions = inspection.interactions;
  const waitingChild = run.data.child ?? null;
  const showChildren = childRuns.length > 0 || initialTab === "children";
  const sessionId = givenSessionId ?? run.data.session_id;
  return (
    <div className="flex flex-col gap-4">
      {!nested && run.data.parent_run_id ? (
        <ParentCrumb parentRunId={run.data.parent_run_id} depth={run.data.depth} />
      ) : null}
      {kind === "node_child" ? (
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
          <Bot className="size-3.5" aria-hidden />
          {t("inspector.nodeChild")}
          <Link
            to={sessionLogPath(run.data.session_id, runId)}
            className="text-accent hover:underline"
          >
            {t("inspector.openSession")}
          </Link>
        </p>
      ) : null}
      <RunSummary run={run.data} tree={tree.data} />
      {live ? <Alert tone="info" title={t("inspector.live")} /> : null}
      {waitingChild ? (
        <Alert
          tone="info"
          title={t("inspector.waitingChild", { name: waitingChild.agent_name })}
          action={
            <Link
              to={sessionLogPath(waitingChild.session_id, waitingChild.run_id)}
              className="text-sm text-accent hover:underline"
            >
              {t("inspector.openChild")}
            </Link>
          }
        />
      ) : null}
      {run.data.status === "waiting_input" ? (
        <Alert tone="warn" title={t("inspector.waiting")}>
          {t("inspector.waitingHint")}
        </Alert>
      ) : null}
      <Tabs defaultValue={initialTab}>
        <TabsList aria-label={t("inspector.tabs")}>
          <TabsTrigger value="steps">{t("inspector.stepsTab")}</TabsTrigger>
          {showChildren ? (
            <TabsTrigger value="children">
              {t("inspector.childrenTab", { count: childRuns.length })}
            </TabsTrigger>
          ) : null}
          <TabsTrigger value="approvals">
            {t("inspector.approvalsTab", { count: interactions.length })}
          </TabsTrigger>
          <TabsTrigger value="events">
            {t("inspector.eventsTab", { count: events.data.items.length })}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="steps" className="pt-3">
          {inspection.preamble.length > 0 ? (
            <div className="mb-3 flex flex-col gap-1">
              {inspection.preamble.map((notice) => (
                <NoticeLine key={notice.seq} notice={notice} />
              ))}
            </div>
          ) : null}
          {inspection.steps.length === 0 ? (
            <p className="text-sm text-ink-muted">{t("inspector.noSteps")}</p>
          ) : (
            <ol className="flex flex-col gap-5">
              {inspection.steps.map((step, index) => (
                <StepCard
                  key={`${step.step}-${index}`}
                  runId={runId}
                  step={step}
                  focusToolCallId={focusToolCallId}
                />
              ))}
            </ol>
          )}
          {inspection.files.length > 0 ? (
            <section className="mt-4">
              <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-ink-muted">
                <FileDiff className="size-3.5" aria-hidden />
                {t("inspector.files")}
              </h4>
              <ul className="flex flex-col gap-1">
                {inspection.files.map((file) => (
                  <li key={file.path} className="flex items-center gap-2 text-xs">
                    <span className="min-w-0 flex-1 truncate font-mono text-ink">{file.path}</span>
                    <span className="shrink-0 text-ink-subtle tabular-nums">
                      {formatBytes(file.size)}
                    </span>
                    {file.downloadable && file.sessionFileId ? (
                      <a
                        href={sessionFileUrl(sessionId, file.sessionFileId)}
                        download
                        className="inline-flex shrink-0 items-center gap-1 text-accent hover:underline"
                        aria-label={t("timeline.download", { name: file.path })}
                      >
                        <Download className="size-3.5" aria-hidden />
                      </a>
                    ) : (
                      <span className="shrink-0 text-ink-subtle">
                        {t(keyOf(FILE_REASON_KEYS, file.reason) ?? "timeline.notStored")}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </TabsContent>
        <TabsContent value="approvals" className="pt-3">
          {interactions.length === 0 ? (
            <p className="text-sm text-ink-muted">{t("inspector.noApprovals")}</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {interactions.map((interaction) => (
                <li
                  key={interaction.interactionId}
                  className="rounded-md border border-line px-3 py-2"
                >
                  <p className="mb-1 flex items-center gap-2 text-xs text-ink-muted">
                    <Badge>{t(`interaction.kind.${interaction.kind}`)}</Badge>
                    {interaction.toolName ? (
                      <span className="font-mono">{interaction.toolName}</span>
                    ) : null}
                    {interaction.requestedAt ? (
                      <span>{formatDateTime(interaction.requestedAt)}</span>
                    ) : null}
                  </p>
                  <InteractionLine interaction={interaction} />
                </li>
              ))}
            </ul>
          )}
        </TabsContent>
        <TabsContent value="events" className="pt-3">
          <EventTable events={events.data.items} truncated={events.data.truncated} />
        </TabsContent>
        {showChildren ? (
          <TabsContent value="children" className="pt-3">
            {children.isPending ? (
              <Spinner label={t("children.loading")} />
            ) : children.isError ? (
              <ErrorAlert
                error={children.error}
                title={t("children.loadFailed")}
                onRetry={() => void children.refetch()}
              />
            ) : (
              <ChildRunList runs={childRuns} />
            )}
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}
