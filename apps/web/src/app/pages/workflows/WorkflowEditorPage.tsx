import { useQueryClient } from "@tanstack/react-query";
import { PanelRight, Play, Rocket, ScrollText } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useBlocker, useParams, useSearchParams } from "react-router";
import { isApiError } from "@/api/http";
import type { WfIssue, WorkflowOut, WorkflowVersionOut } from "@/api/types";
import { ContextBarActions } from "@/app/layout/contextBar";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { ConversationVarPanel } from "@/components/flow/ConversationVarPanel";
import { DslMenu } from "@/components/flow/DslMenu";
import { IssuesPopover, SaveStatus } from "@/components/flow/EditorStatus";
import { EnvVarPanel } from "@/components/flow/EnvVarPanel";
import { FlowCanvas, FlowCanvasProvider } from "@/components/flow/FlowCanvas";
import { NodeInspector } from "@/components/flow/NodeInspector";
import { NodePalette } from "@/components/flow/NodePalette";
import {
  ChatflowPreview,
  useChatflowPreview,
  type ChatTab,
} from "@/components/flow/run/ChatflowPreview";
import { RunPanel, type CurrentRun } from "@/components/flow/run/RunPanel";
import { SingleRunDialog } from "@/components/flow/run/SingleRunDialog";
import { WebappSettingsForm } from "@/components/flow/WebappSettingsForm";
import { WorkflowApiPanel } from "@/components/flow/WorkflowApiPanel";
import { WorkflowPublishPanel } from "@/components/flow/WorkflowPublishPanel";
import { WorkflowSettingsPanel } from "@/components/flow/WorkflowSettingsPanel";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { PageSpinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import {
  draftRunRequest,
  useWorkflow,
  useWorkflowDraft,
  validateDraft,
  workflowKeys,
} from "@/features/workflows/api";
import type { ChatflowSession } from "@/features/workflows/chatflowSession";
import { createFlowStore, isDirty, type FlowStore } from "@/features/workflows/flowStore";
import {
  FlowStoreProvider,
  useFlow,
  useFlowActions,
  useFlowStoreApi,
} from "@/features/workflows/flowStoreContext";
import { nodeById, toGraphPayload } from "@/features/workflows/graph";
import "@/features/workflows/i18n";
import {
  serverInputErrors,
  startFieldsOf,
  toRunInputs,
  type FieldValue,
} from "@/features/workflows/runInputs";
import { useDraftAutosave } from "@/features/workflows/useDraftAutosave";
import {
  FlowRunUiProvider,
  WfRunProvider,
  type FlowRunUi,
} from "@/features/workflows/wfRunContext";
import { consoleWfRunApi, WfRunController } from "@/features/workflows/wfRunController";
import { isWfRunOpen } from "@/features/workflows/wfRunStore";
import { errorMessage } from "@/lib/errors";

const PANEL_TABS = ["node", "run", "variables", "settings", "publish", "api"] as const;
type PanelTab = (typeof PANEL_TABS)[number];

// 입력 칸·코드 편집기 안의 Ctrl+Z는 그 칸의 실행 취소다. 캔버스 실행 취소는 그 밖에서만.
function isEditable(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    target.closest("input, textarea, select, [contenteditable='true'], .cm-editor") !== null
  );
}

function EditorBody({
  workflow,
  controller,
  chat,
}: {
  workflow: WorkflowOut;
  controller: WfRunController;
  // 챗플로우의 미리보기 대화. 워크플로우면 null이다.
  chat: ChatflowSession | null;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const store = useFlowStoreApi();
  const actions = useFlowActions();
  const autosave = useDraftAutosave(store, workflow.id);
  const { flush } = autosave;
  const [tab, setTab] = useState<PanelTab>("node");
  const [paletteOpen, setPaletteOpen] = useState(true);
  const [panelOpen, setPanelOpen] = useState(true);
  const [leaveFailed, setLeaveFailed] = useState(false);
  const [, setSearchParams] = useSearchParams();
  const dirty = useFlow(isDirty);
  const kind = useFlow((state) => state.kind);
  const loadCount = useFlow((state) => state.loadCount);
  const envVars = useFlow((state) => state.envVars);
  const conversationVars = useFlow((state) => state.conversationVars);
  const settings = useFlow((state) => state.settings);
  const graph = useFlow((state) => state.graph);
  const fields = useMemo(() => startFieldsOf(graph), [graph]);
  // 실행 패널의 입력·오류·현재 런 종류. 패널 탭을 옮겨도 남게 여기 둔다.
  const [runValues, setRunValues] = useState<Record<string, FieldValue>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [startError, setStartError] = useState<unknown>(null);
  const [currentRun, setCurrentRun] = useState<CurrentRun>({ kind: "draft" });
  const [singleNode, setSingleNode] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [chatTab, setChatTab] = useState<ChatTab>("chat");

  function showRunPanel(sub?: ChatTab) {
    setPanelOpen(true);
    setTab("run");
    if (sub) setChatTab(sub);
  }

  // 실행 전에 항상 저장한다(설계 7.3). 저장하지 못하면 실행하지 않는다.
  async function runDraft() {
    const { inputs, problems } = toRunInputs(fields, runValues);
    const names = Object.keys(problems);
    if (names.length > 0) {
      setFieldErrors(
        Object.fromEntries(
          names.map((name) => [
            name,
            problems[name] === "number" ? t("run.invalidNumber") : t("run.required"),
          ]),
        ),
      );
      showRunPanel();
      return;
    }
    setFieldErrors({});
    setStartError(null);
    setFocusId(null);
    if (!(await flush())) {
      setStartError(new Error(t("run.saveFailed")));
      return;
    }
    setCurrentRun({ kind: "draft" });
    const result = await controller.start(() =>
      draftRunRequest(workflow.id, { inputs, response_mode: "streaming" }),
    );
    if (result === true) return;
    if (isApiError(result, 422) && result.code === "invalid_inputs") {
      setFieldErrors(serverInputErrors(result.details));
    }
    if (
      isApiError(result, 422) &&
      result.code === "draft_invalid" &&
      Array.isArray(result.details)
    ) {
      actions.setIssues(result.details as WfIssue[]);
    }
    setStartError(result);
  }

  // 상단 실행 단추: 실행 탭을 열고, 입력 칸이 없으면 바로 돌린다. 챗플로우는 미리보기 대화를 연다.
  function onTopRun() {
    if (chat) {
      showRunPanel("chat");
      return;
    }
    showRunPanel();
    const busy = isWfRunOpen(controller.store.getState().status);
    if (fields.length === 0 && !busy) void runDraft();
  }

  // 미리보기 메시지도 실행처럼 먼저 저장한다. 저장하지 못하면 보내지 않는다.
  async function beforeChatSend(): Promise<true | unknown> {
    setStartError(null);
    setFocusId(null);
    setCurrentRun({ kind: "draft" });
    return (await flush()) ? true : new Error(t("chat.saveFailed"));
  }

  function onChatSendError(error: unknown): boolean {
    if (isApiError(error, 422) && error.code === "draft_invalid" && Array.isArray(error.details)) {
      actions.setIssues(error.details as WfIssue[]);
    }
    return false;
  }

  const runUi = useMemo<FlowRunUi>(
    () => ({
      requestSingleRun: (nodeId) => {
        // 노드 하나 실행은 같은 컨트롤러를 쓴다. 대화 턴이 도는 중에 시작하면 그 턴을 놓친다.
        const turn = chat?.chat.getState().openRunId ?? null;
        if (turn !== null && controller.runId === turn) {
          toast.warning(t("chat.singleBusy"));
          return;
        }
        setSingleNode(nodeId);
      },
      focusNodeRun: (nodeRunId) => {
        setFocusId(nodeRunId);
        setPanelOpen(true);
        setTab("run");
        setChatTab("nodes");
      },
    }),
    [chat, controller, t],
  );

  // 노드를 고르면 노드 탭을 열고, 주소의 ?node=에 남긴다(설계 7.2).
  useEffect(
    () =>
      store.subscribe((state, previous) => {
        if (state.selection === previous.selection) return;
        const single = state.selection.length === 1 ? state.selection[0] : null;
        if (state.selection.length > 0) {
          setTab("node");
          setPanelOpen(true);
        }
        setSearchParams(
          (params) => {
            const next = new URLSearchParams(params);
            if (single) next.set("node", single);
            else next.delete("node");
            return next;
          },
          { replace: true },
        );
      }),
    [store, setSearchParams],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey;
      if (!mod) return;
      const key = event.key.toLowerCase();
      if (key === "s") {
        event.preventDefault();
        void flush();
      } else if (key === "z" && !isEditable(event.target)) {
        event.preventDefault();
        if (event.shiftKey) actions.redo();
        else actions.undo();
      } else if (key === "y" && !isEditable(event.target)) {
        event.preventDefault();
        actions.redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [flush, actions]);

  // 미저장 이탈 경고(1단계 빌더와 같다). 다른 화면으로 가려 하면 먼저 저장을 해 보고, 저장되면 그대로 간다.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty(store.getState()) && currentLocation.pathname !== nextLocation.pathname,
  );
  const blocked = blocker.state === "blocked";
  // useBlocker는 그릴 때마다 새 객체를 돌려준다. 저장이 끝난 뒤에 부를 최신 것을 둔다.
  const blockerRef = useRef(blocker);
  useEffect(() => {
    blockerRef.current = blocker;
  });
  useEffect(() => {
    if (!blocked) return;
    let cancelled = false;
    void flush().then((ok) => {
      if (cancelled) return;
      if (ok) blockerRef.current.proceed?.();
      else setLeaveFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [blocked, flush]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  async function revalidate() {
    const state = store.getState();
    try {
      const result = await validateDraft(workflow.id, {
        graph: { ...toGraphPayload(state.graph), viewport: state.viewport },
        env_vars: state.envVars.map((row) => ({
          name: row.name,
          secret: row.secret,
          value: row.keep && row.value === "" ? null : row.value,
        })),
        conversation_vars: kind === "chatflow" ? state.conversationVars : [],
      });
      actions.setIssues(result.issues);
    } catch (error) {
      toast.error(errorMessage(error, tc));
    }
  }

  async function reload() {
    try {
      await autosave.reload();
      toast.success(t("editor.reloaded"));
    } catch (error) {
      toast.error(errorMessage(error, tc));
    }
  }

  return (
    <div className="flex h-full min-h-0">
      <ContextBarActions>
        <Badge tone={kind === "chatflow" ? "info" : "neutral"}>{t(`kind.${kind}`)}</Badge>
        <SaveStatus autosave={autosave} />
        <IssuesPopover
          onSelectNode={(nodeId) => actions.select([nodeId])}
          onRevalidate={revalidate}
        />
        <Button asChild size="sm" variant="ghost">
          <Link to={`/workflows/${workflow.id}/logs`}>
            <ScrollText className="size-3.5" aria-hidden />
            {t("editor.logs")}
          </Link>
        </Button>
        <DslMenu workflow={workflow} autosave={autosave} />
        <Button size="sm" variant="secondary" onClick={onTopRun}>
          <Play className="size-3.5" aria-hidden />
          {chat ? t("editor.preview") : t("editor.run")}
        </Button>
        <Button
          size="sm"
          variant="primary"
          onClick={() => {
            setPanelOpen(true);
            setTab("publish");
          }}
        >
          <Rocket className="size-3.5" aria-hidden />
          {t("editor.publish")}
        </Button>
        <Button
          size="iconSm"
          variant="ghost"
          aria-pressed={panelOpen}
          aria-label={panelOpen ? t("panel.close") : t("panel.open")}
          title={panelOpen ? t("panel.close") : t("panel.open")}
          onClick={() => setPanelOpen((open) => !open)}
        >
          <PanelRight className="size-4" aria-hidden />
        </Button>
      </ContextBarActions>
      <NodePalette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <div className="relative min-w-0 flex-1">
        {autosave.status === "conflict" ? (
          <div className="absolute top-3 right-3 left-3 z-10">
            <Alert
              tone="warn"
              title={t("editor.conflictTitle")}
              action={
                <Button size="sm" onClick={() => void reload()}>
                  {t("editor.reload")}
                </Button>
              }
            >
              {t("editor.conflictBody")}
            </Alert>
          </div>
        ) : autosave.status === "failed" ? (
          <div className="absolute top-3 right-3 left-3 z-10">
            <Alert
              tone="danger"
              title={t("editor.saveFailed", { message: errorMessage(autosave.error, tc) })}
              action={
                <Button size="sm" onClick={() => void autosave.flush()}>
                  {tc("actions.retry")}
                </Button>
              }
            />
          </div>
        ) : null}
        <FlowRunUiProvider ui={runUi}>
          <FlowCanvas />
        </FlowRunUiProvider>
      </div>
      {panelOpen ? (
        <aside
          aria-label={t("panel.label")}
          className="flex w-[26rem] shrink-0 flex-col border-l border-line bg-surface"
        >
          <Tabs
            value={tab}
            onValueChange={(value) => setTab(value as PanelTab)}
            className="flex min-h-0 flex-1 flex-col"
          >
            <TabsList aria-label={t("panel.label")} className="shrink-0 px-2">
              {PANEL_TABS.map((key) => (
                <TabsTrigger key={key} value={key}>
                  {chat && key === "run" ? t("panel.tabs.preview") : t(`panel.tabs.${key}`)}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="node" className="min-h-0 flex-1 overflow-y-auto">
              <NodeInspector />
            </TabsContent>
            {chat ? (
              <TabsContent value="run" className="flex min-h-0 flex-1 flex-col">
                <ChatflowPreview
                  workflow={workflow}
                  session={chat}
                  tab={chatTab}
                  onTabChange={setChatTab}
                  current={currentRun}
                  focusId={focusId}
                  beforeSend={beforeChatSend}
                  onSendError={onChatSendError}
                />
              </TabsContent>
            ) : (
              <TabsContent value="run" className="min-h-0 flex-1 overflow-y-auto">
                <RunPanel
                  workflow={workflow}
                  values={runValues}
                  onValuesChange={setRunValues}
                  fieldErrors={fieldErrors}
                  startError={startError}
                  onRun={() => void runDraft()}
                  current={currentRun}
                  focusId={focusId}
                />
              </TabsContent>
            )}
            <TabsContent value="variables" className="min-h-0 flex-1 overflow-y-auto p-4">
              <div className="flex flex-col gap-4">
                <EnvVarPanel key={`env:${loadCount}`} initial={envVars} />
                {kind === "chatflow" ? (
                  <ConversationVarPanel key={`conv:${loadCount}`} initial={conversationVars} />
                ) : null}
              </div>
            </TabsContent>
            <TabsContent value="settings" className="min-h-0 flex-1 overflow-y-auto p-4">
              <div className="flex flex-col gap-4">
                <WorkflowSettingsPanel
                  key={`settings:${loadCount}`}
                  workflow={workflow}
                  settings={settings}
                />
                <WebappSettingsForm key={`webapp:${loadCount}`} initial={settings.webapp} />
              </div>
            </TabsContent>
            <TabsContent value="publish" className="min-h-0 flex-1 overflow-y-auto p-4">
              <WorkflowPublishPanel workflowId={workflow.id} flush={autosave.flush} />
            </TabsContent>
            <TabsContent value="api" className="min-h-0 flex-1 overflow-y-auto p-4">
              <WorkflowApiPanel workflow={workflow} />
            </TabsContent>
          </Tabs>
        </aside>
      ) : null}
      <SingleRunDialog
        workflowId={workflow.id}
        kind={kind}
        nodeId={singleNode}
        onOpenChange={(open) => {
          if (!open) setSingleNode(null);
        }}
        flush={flush}
        onStarted={(nodeId) => {
          setStartError(null);
          setFocusId(null);
          setCurrentRun({ kind: "single", nodeId });
          showRunPanel("nodes");
        }}
      />
      <ConfirmDialog
        open={leaveFailed}
        onOpenChange={(open) => {
          if (open) return;
          setLeaveFailed(false);
          blocker.reset?.();
        }}
        title={t("editor.leaveTitle")}
        description={t("editor.leaveBody")}
        confirmLabel={tc("unsavedDialog.leave")}
        onConfirm={() => {
          setLeaveFailed(false);
          blocker.proceed?.();
        }}
      />
    </div>
  );
}

function Editor({ workflow, draft }: { workflow: WorkflowOut; draft: WorkflowVersionOut }) {
  const { t: tc } = useTranslation();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [store] = useState<FlowStore>(() => {
    const created = createFlowStore(workflow.id, workflow.kind);
    created.getState().actions.load(draft);
    const shared = searchParams.get("node");
    if (shared && nodeById(created.getState().graph, shared)) {
      created.getState().actions.select([shared]);
    }
    return created;
  });
  const [controller] = useState(() => new WfRunController(consoleWfRunApi));
  const chat = useChatflowPreview(workflow, controller);
  useEffect(() => {
    controller.configure({
      onError: (error) => toast.error(errorMessage(error, tc)),
      // 끝난 런이 실행 기록 목록에 바로 보이게 한다.
      onSettled: () =>
        void queryClient.invalidateQueries({
          queryKey: [...workflowKeys.detail(workflow.id), "runs"],
        }),
    });
  });
  useEffect(() => () => controller.dispose(), [controller]);
  return (
    <FlowStoreProvider store={store}>
      <WfRunProvider controller={controller}>
        <FlowCanvasProvider>
          <EditorBody workflow={workflow} controller={controller} chat={chat} />
        </FlowCanvasProvider>
      </WfRunProvider>
    </FlowStoreProvider>
  );
}

// 캔버스 편집기(설계 7.2 /workflows/:id, 7.3). 오른쪽 실행 탭이 미리보기 런과 단일 노드 실행을 보인다.
export default function WorkflowEditorPage() {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const { id = "" } = useParams();
  const workflow = useWorkflow(id);
  const draft = useWorkflowDraft(id);

  if (workflow.isPending || draft.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (workflow.isError || draft.isError) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-6">
        <ErrorAlert
          error={workflow.error ?? draft.error}
          title={t("editor.loadFailed")}
          onRetry={() => {
            void workflow.refetch();
            void draft.refetch();
          }}
        />
      </div>
    );
  }
  return <Editor key={draft.data.id} workflow={workflow.data} draft={draft.data} />;
}
