import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, History, Plus, SearchX } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import { isApiError } from "@/api/http";
import type { FlowAppOut, WfRunOut } from "@/api/types";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { ConnectionNotice } from "@/components/chat/MessageList";
import { RunStatusView } from "@/components/chat/RunStatusBar";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { OutputsView } from "@/components/flow/run/OutputsView";
import { PendingPrompt } from "@/components/flow/run/PendingPrompt";
import { FlowChatApp } from "@/components/flow/webapp/FlowChatApp";
import { FlowRunForm } from "@/components/flow/webapp/FlowRunForm";
import { RunProgress } from "@/components/flow/webapp/RunProgress";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  flowFileUrl,
  flowKeys,
  flowRunRequest,
  uploadFlowFile,
  useFlowApp,
  useFlowRunDetail,
  useFlowRuns,
  webWfRunApi,
} from "@/features/webapp/flows";
import "@/features/workflows/i18n";
import {
  initialValues,
  serverInputErrors,
  toRunInputs,
  type FieldValue,
} from "@/features/workflows/runInputs";
import { WfRunController } from "@/features/workflows/wfRunController";
import { finishedNodeCount, isWfRunOpen, runningNode } from "@/features/workflows/wfRunStore";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";

// 기록 목록에 보일 입력 요약. 첫 글 입력이고 없으면 비운다.
function inputSummary(run: WfRunOut): string {
  for (const value of Object.values(run.inputs)) {
    if (typeof value === "string" && value.trim()) return value.trim().slice(0, 80);
  }
  return "";
}

function RunHistory({
  app,
  selected,
  onSelect,
  onNew,
}: {
  app: FlowAppOut;
  selected: string | null;
  onSelect: (runId: string) => void;
  onNew: () => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tl } = useTranslation("logs");
  const runs = useFlowRuns(app.slug);
  const now = useNow();
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  let list;
  if (runs.isPending) {
    list = <Spinner className="mx-2 my-3" label={t("flowApp.history")} />;
  } else if (runs.isError) {
    list = (
      <ErrorAlert
        error={runs.error}
        title={t("flowApp.historyFailed")}
        onRetry={() => void runs.refetch()}
      />
    );
  } else if (items.length === 0) {
    list = <p className="px-2 py-3 text-sm text-ink-muted">{t("flowApp.historyEmpty")}</p>;
  } else {
    list = (
      <ul className="flex flex-col gap-0.5">
        {items.map((run) => (
          <li key={run.id}>
            <button
              type="button"
              aria-current={run.id === selected ? "true" : undefined}
              className={cn(
                "flex w-full flex-col gap-0.5 rounded-md px-2.5 py-2 text-left hover:bg-hover",
                run.id === selected && "bg-accent-soft",
              )}
              onClick={() => onSelect(run.id)}
            >
              <span className="flex items-center gap-2">
                <span className="truncate text-sm text-ink">
                  {inputSummary(run) || t("flowApp.noInputSummary")}
                </span>
              </span>
              <span className="flex items-center gap-2 text-xs text-ink-subtle">
                <span>
                  {formatRelative(run.queued_at, Math.max(now, Date.parse(run.queued_at)))}
                </span>
                <span>{tl(`runStatus.${run.status}`)}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <aside
      aria-label={t("flowApp.history")}
      className="flex min-h-0 flex-col border-l border-line bg-surface"
    >
      <div className="flex items-center gap-2 px-4 pt-4 pb-2">
        <History className="size-4 text-ink-muted" aria-hidden />
        <h2 className="text-sm font-semibold text-ink">{t("flowApp.history")}</h2>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={onNew}>
          <Plus className="size-3.5" aria-hidden />
          {t("flowApp.new")}
        </Button>
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {list}
        {runs.hasNextPage ? (
          <Button
            size="sm"
            variant="ghost"
            className="mt-1 w-full"
            loading={runs.isFetchingNextPage}
            onClick={() => void runs.fetchNextPage()}
          >
            {t("flowApp.more")}
          </Button>
        ) : null}
      </nav>
    </aside>
  );
}

// 워크플로우 웹앱 하나(설계 7.4): 입력 폼 → 실행 → 진행 → 결과, 오른쪽에 내 실행 기록. 고른 런은 ?run=이다.
function FlowApp({ app }: { app: FlowAppOut }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const selected = searchParams.get("run");
  const [controller] = useState(() => new WfRunController(webWfRunApi));
  const state = useStore(
    controller.store,
    useShallow((s) => s),
  );
  const meta = useStore(
    controller.meta,
    useShallow((m) => m),
  );
  const [values, setValues] = useState<Record<string, FieldValue>>(() => initialValues(app.fields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [startError, setStartError] = useState<unknown>(null);
  const open = isWfRunOpen(state.status);
  const finished = state.runId !== null && !open && state.status !== "idle";
  const detail = useFlowRunDetail(finished && state.outputsTruncated ? state.runId : null);
  const title = app.webapp.title || app.name;

  useEffect(() => {
    controller.configure({
      onError: (error) => toast.error(errorMessage(error, tc)),
      onSettled: () => void queryClient.invalidateQueries({ queryKey: flowKeys.runs(app.slug) }),
    });
  });
  useEffect(() => () => controller.dispose(), [controller]);
  // 기록에서 고른 런에 붙는다. 방금 시작한 런은 이미 붙어 있다.
  useEffect(() => {
    if (selected && selected !== controller.runId) controller.attach(selected);
  }, [selected, controller]);

  async function run() {
    const { inputs, problems } = toRunInputs(app.fields, values);
    const names = Object.keys(problems);
    if (names.length > 0) {
      setErrors(
        Object.fromEntries(
          names.map((name) => [
            name,
            problems[name] === "number" ? t("run.invalidNumber") : t("run.required"),
          ]),
        ),
      );
      return;
    }
    setErrors({});
    setStartError(null);
    const result = await controller.start(() => flowRunRequest(app.slug, inputs));
    if (result === true) {
      const runId = controller.runId;
      if (runId) setSearchParams({ run: runId }, { replace: true });
      void queryClient.invalidateQueries({ queryKey: flowKeys.runs(app.slug) });
      return;
    }
    if (isApiError(result, 422) && result.code === "invalid_inputs") {
      setErrors(serverInputErrors(result.details));
    }
    setStartError(result);
  }

  function startNew() {
    controller.reset();
    setStartError(null);
    setSearchParams({}, { replace: true });
  }

  const pendingTitle = state.pendingInteraction
    ? (Object.values(state.nodeRuns).find(
        (item) => item.nodeId === state.pendingInteraction?.nodeId,
      )?.title ?? state.pendingInteraction.nodeId)
    : "";

  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_18rem] bg-canvas">
      <main aria-label={title} className="flex min-h-0 min-w-0 flex-col">
        <header className="flex items-center gap-3 border-b border-line bg-surface px-5 py-3">
          <AgentIcon icon={app.icon} name={app.name} className="size-8 text-base" />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-ink">{title}</h1>
            {app.webapp.description || app.description ? (
              <p className="truncate text-xs text-ink-muted">
                {app.webapp.description || app.description}
              </p>
            ) : null}
          </div>
          <Badge tone="accent" title={t("flowApp.versionHint", { version: app.version_number })}>
            {t("flowApp.version", { version: app.version_number })}
          </Badge>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-5 py-5">
            <section
              aria-label={t("run.inputs")}
              className="rounded-lg border border-line bg-surface p-4"
            >
              <FlowRunForm
                fields={app.fields}
                values={values}
                onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
                errors={errors}
                upload={(file) => uploadFlowFile(app.slug, file)}
                running={open}
                starting={meta.starting}
                stopping={meta.stopping}
                onRun={() => void run()}
                onStop={() => void controller.stop()}
              />
            </section>
            {startError ? <ErrorAlert error={startError} title={t("run.startFailed")} /> : null}
            {state.runId ? (
              <section
                aria-label={t("run.result")}
                className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-4"
              >
                <RunStatusView
                  kind="workflow"
                  values={{
                    status: state.status,
                    step: finishedNodeCount(state),
                    stepLimit: null,
                    usage: state.usage,
                    model: null,
                    queuePosition: state.queuePosition,
                    startedAt: state.startedAt,
                    endedAt: state.endedAt,
                    activity: open ? runningNode(state) : null,
                  }}
                />
                {meta.connection ? (
                  <ConnectionNotice
                    status={meta.connection.status}
                    attempt={meta.connection.attempt}
                  />
                ) : null}
                {state.error ? (
                  <Alert tone="danger" title={t("run.error", { code: state.error.code })}>
                    {state.error.message}
                  </Alert>
                ) : null}
                {state.pendingInteraction && state.status === "waiting_input" ? (
                  <PendingPrompt
                    pending={state.pendingInteraction}
                    nodeTitle={pendingTitle}
                    busy={meta.responding}
                    onRespond={(input) => void controller.respond(input)}
                    upload={(file) => uploadFlowFile(app.slug, file)}
                    onSubmitForm={(answers) => controller.submitForm(answers)}
                  />
                ) : null}
                <RunProgress state={state} />
                {state.status === "succeeded" ? (
                  <div className="border-t border-line pt-3">
                    <h2 className="mb-2 text-sm font-semibold text-ink">{t("run.outputs")}</h2>
                    <OutputsView
                      outputs={detail.data?.outputs ?? state.outputs}
                      truncated={state.outputsTruncated && !detail.data}
                      fileUrl={(fileId) => (state.runId ? flowFileUrl(state.runId, fileId) : null)}
                    />
                  </div>
                ) : null}
              </section>
            ) : null}
          </div>
        </div>
        <footer className="border-t border-line px-5 py-2">
          <Link
            to="/workflows"
            className="inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink"
          >
            <ArrowLeft className="size-3.5" aria-hidden />
            {t("flowApp.console")}
          </Link>
        </footer>
      </main>
      <RunHistory
        app={app}
        selected={state.runId}
        onSelect={(runId) => setSearchParams({ run: runId }, { replace: true })}
        onNew={startNew}
      />
    </div>
  );
}

export default function FlowAppPage() {
  const { t } = useTranslation("workflows");
  const { slug = "" } = useParams();
  const app = useFlowApp(slug);
  if (app.isPending) return <PageSpinner label={t("flowApp.loading")} />;
  if (app.isError) {
    if (isApiError(app.error, 404)) {
      return (
        <div className="mx-auto max-w-xl p-8">
          <EmptyState
            icon={SearchX}
            title={t("flowApp.notFound")}
            hint={t("flowApp.notFoundHint")}
            action={
              <Button asChild variant="primary">
                <Link to="/workflows">{t("flowApp.console")}</Link>
              </Button>
            }
          />
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-xl p-8">
        <ErrorAlert
          error={app.error}
          title={t("flowApp.loadFailed")}
          onRetry={() => void app.refetch()}
        />
      </div>
    );
  }
  // 다른 웹앱으로 옮겨 가면 상태를 새로 만든다. 챗플로우는 채팅(대화는 주소의 /c/:cid)이다.
  if (app.data.kind === "chatflow") {
    return <FlowChatApp key={app.data.workflow_id} app={app.data} />;
  }
  return <FlowApp key={app.data.workflow_id} app={app.data} />;
}
