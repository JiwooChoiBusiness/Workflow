import { useQueryClient } from "@tanstack/react-query";
import { Ellipsis, MessageSquarePlus, Pencil, RefreshCw, ScrollText, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { WorkflowOut } from "@/api/types";
import { ConnectionNotice } from "@/components/chat/MessageList";
import { RunStatusView } from "@/components/chat/RunStatusBar";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { JsonTree } from "@/components/common/JsonTree";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/layout";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import { formatPast } from "@/features/logs/time";
import { uploadWorkflowFile } from "@/features/workflows/api";
import {
  chatKeys,
  consoleChatApi,
  createPreviewConversation,
  deleteConversation,
  renameConversation,
  useConversationVariables,
  usePreviewConversations,
} from "@/features/workflows/chatApi";
import { ChatflowSession } from "@/features/workflows/chatflowSession";
import { useFlow } from "@/features/workflows/flowStoreContext";
import "@/features/workflows/i18n";
import { wfRunPath } from "@/features/workflows/runLogFilters";
import { startFieldsOf } from "@/features/workflows/runInputs";
import {
  useWfRun as useWfRunState,
  useWfRunController,
  useWfRunMeta,
} from "@/features/workflows/wfRunContext";
import type { WfRunController } from "@/features/workflows/wfRunController";
import { finishedNodeCount, isWfRunOpen, runningNode } from "@/features/workflows/wfRunStore";
import { errorMessage } from "@/lib/errors";
import { useNow } from "@/lib/useNow";
import { ChatflowChat } from "../chat/ChatflowChat";
import { ConversationTitleDialog } from "../chat/ConversationTitleDialog";
import { NodeTimeline } from "./NodeTimeline";
import { PendingPrompt } from "./PendingPrompt";
import { pendingInCard, useTimelineParts, type CurrentRun } from "./RunPanel";

export const CHAT_TABS = ["chat", "nodes", "variables"] as const;
export type ChatTab = (typeof CHAT_TABS)[number];

const NEW = "__new__";

// 새로 고쳐도 같은 미리보기 대화를 다시 열려고 워크플로우별로 브라우저에 둔다.
const storageKey = (workflowId: string) => `wf.chatflow-preview.${workflowId}`;

function loadConversationId(workflowId: string): string | null {
  try {
    return window.localStorage.getItem(storageKey(workflowId));
  } catch {
    return null;
  }
}

function saveConversationId(workflowId: string, conversationId: string | null): void {
  try {
    if (conversationId) window.localStorage.setItem(storageKey(workflowId), conversationId);
    else window.localStorage.removeItem(storageKey(workflowId));
  } catch {
    // 저장하지 못해도 이번 화면에서는 계속 쓴다.
  }
}

/*
 * 편집기의 챗플로우 미리보기 대화(설계 7.3 ChatflowPreview). 실행 탭을 옮겨도 대화가 이어지도록 편집기가
 * 들고 있는다. 런은 실행 패널·캔버스와 같은 컨트롤러로 따라간다. 워크플로우(kind=workflow)면 null이다.
 */
export function useChatflowPreview(
  workflow: WorkflowOut,
  controller: WfRunController,
): ChatflowSession | null {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [session] = useState(() =>
    workflow.kind === "chatflow"
      ? new ChatflowSession(consoleChatApi(workflow.id), controller)
      : null,
  );
  const workflowId = workflow.id;
  useEffect(() => {
    session?.configure({
      createConversation: () => createPreviewConversation(workflowId),
      onConversationChange: (conversationId) => {
        saveConversationId(workflowId, conversationId);
        void queryClient.invalidateQueries({ queryKey: chatKeys.preview(workflowId) });
      },
      onSent: () => void queryClient.invalidateQueries({ queryKey: chatKeys.preview(workflowId) }),
      // 실행 기록 목록은 편집기의 런 컨트롤러가 다시 읽게 한다.
      onSettled: (conversationId) => {
        void queryClient.invalidateQueries({ queryKey: chatKeys.preview(workflowId) });
        void queryClient.invalidateQueries({ queryKey: chatKeys.variables(conversationId) });
      },
      onError: (error) => toast.error(errorMessage(error, t)),
    });
  });
  useEffect(() => {
    if (!session) return;
    void session.open(session.conversationId ?? loadConversationId(workflowId));
    return () => session.dispose();
  }, [session, workflowId]);
  return session;
}

function ConversationBar({
  workflow,
  session,
}: {
  workflow: WorkflowOut;
  session: ChatflowSession;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const queryClient = useQueryClient();
  const conversations = usePreviewConversations(workflow.id);
  const current = useStore(session.meta, (meta) => meta.conversationId);
  const now = useNow();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [removing, setRemoving] = useState(false);
  const items = conversations.data ?? [];
  const selected = items.find((item) => item.id === current) ?? null;
  const untitled = t("chat.untitled");

  const options = [
    { value: NEW, label: t("chat.newConversation") },
    ...items.map((item) => ({
      value: item.id,
      label: item.title || untitled,
      hint: t("chat.conversationHint", {
        time: formatPast(item.last_message_at ?? item.created_at, now),
        count: Math.floor(item.message_count / 2),
      }),
    })),
    // 방금 만든 대화는 목록을 다시 읽기 전까지 목록에 없다.
    ...(current && !selected ? [{ value: current, label: untitled }] : []),
  ];

  const refresh = () => queryClient.invalidateQueries({ queryKey: chatKeys.preview(workflow.id) });

  async function remove() {
    if (!current) return;
    setRemoving(true);
    try {
      await deleteConversation(current);
      session.startNew();
      toast.success(t("chat.deleted"));
      void refresh();
      setDeleting(false);
    } catch (error) {
      toast.error(errorMessage(error, tc));
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
      <Select
        aria-label={t("chat.conversation")}
        className="min-w-0 flex-1"
        value={current ?? NEW}
        options={options}
        onValueChange={(value) => {
          if (value === NEW) {
            session.startNew();
            return;
          }
          saveConversationId(workflow.id, value);
          void session.open(value);
        }}
      />
      <Button
        size="iconSm"
        variant="ghost"
        aria-label={t("chat.newConversation")}
        title={t("chat.newConversation")}
        disabled={!current}
        onClick={() => session.startNew()}
      >
        <MessageSquarePlus className="size-4" aria-hidden />
      </Button>
      <Menu>
        <MenuTrigger asChild>
          <Button
            size="iconSm"
            variant="ghost"
            aria-label={t("chat.menu")}
            title={t("chat.menu")}
            disabled={!current}
          >
            <Ellipsis className="size-4" aria-hidden />
          </Button>
        </MenuTrigger>
        <MenuContent align="end">
          <MenuItem onSelect={() => setRenaming(true)}>
            <Pencil className="size-3.5" aria-hidden />
            {t("chat.rename")}
          </MenuItem>
          {current ? (
            <MenuItem asChild>
              <Link
                to={`/workflows/${encodeURIComponent(workflow.id)}/logs?tab=conversations&conversation=${encodeURIComponent(current)}`}
              >
                <ScrollText className="size-3.5" aria-hidden />
                {t("chat.openLog")}
              </Link>
            </MenuItem>
          ) : null}
          <MenuSeparator />
          <MenuItem tone="danger" onSelect={() => setDeleting(true)}>
            <Trash2 className="size-3.5" aria-hidden />
            {t("chat.delete")}
          </MenuItem>
        </MenuContent>
      </Menu>
      <ConversationTitleDialog
        open={renaming}
        onOpenChange={setRenaming}
        initial={selected?.title ?? ""}
        onSave={async (title) => {
          if (!current) return;
          await renameConversation(current, title);
          await refresh();
          setRenaming(false);
          toast.success(t("chat.renamed"));
        }}
      />
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={t("chat.deleteTitle")}
        description={t("chat.deleteBody", { name: selected?.title || untitled })}
        confirmLabel={tc("actions.delete")}
        pending={removing}
        onConfirm={() => void remove()}
      />
    </div>
  );
}

// 지금 컨트롤러가 따라가는 런(대화의 턴이나 노드 하나 실행)의 노드 타임라인.
function NodesPanel({
  workflow,
  current,
  focusId,
}: {
  workflow: WorkflowOut;
  current: CurrentRun;
  focusId: string | null;
}) {
  const { t } = useTranslation("workflows");
  const controller = useWfRunController();
  const state = useWfRunState(useShallow((s) => s));
  const meta = useWfRunMeta(useShallow((m) => m));
  const { nodeInfo, titleOf, renderChild } = useTimelineParts();
  const open = isWfRunOpen(state.status);
  if (state.runId === null) {
    return <EmptyState title={t("run.empty")} hint={t("chat.nodesEmptyHint")} />;
  }
  return (
    <section aria-label={t("run.result")} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        {current.kind === "single" ? (
          <p className="text-xs font-medium text-ink-muted">
            {t("run.singleTitle", { name: titleOf(current.nodeId) })}
          </p>
        ) : null}
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
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
          </div>
          <Button asChild variant="ghost" size="sm">
            <Link to={wfRunPath(workflow.id, state.runId)}>
              <ScrollText className="size-3.5" aria-hidden />
              {t("run.openLog")}
            </Link>
          </Button>
        </div>
      </div>
      {meta.connection ? (
        <ConnectionNotice status={meta.connection.status} attempt={meta.connection.attempt} />
      ) : null}
      {state.error ? (
        <Alert tone="danger" title={t("run.error", { code: state.error.code })}>
          {state.error.nodeId ? `${titleOf(state.error.nodeId)}: ` : ""}
          {state.error.message}
        </Alert>
      ) : null}
      {state.pendingInteraction && state.status === "waiting_input" ? (
        pendingInCard(state) ? (
          <Alert
            tone="warn"
            title={t("run.pendingInCard", { node: titleOf(state.pendingInteraction.nodeId) })}
          />
        ) : (
          <PendingPrompt
            pending={state.pendingInteraction}
            nodeTitle={titleOf(state.pendingInteraction.nodeId)}
            busy={meta.responding}
            onRespond={(input) => void controller.respond(input)}
            upload={(file) => uploadWorkflowFile(workflow.id, file)}
            onSubmitForm={(values) => controller.submitForm(values)}
          />
        )
      ) : null}
      <NodeTimeline state={state} nodeInfo={nodeInfo} focusId={focusId} renderChild={renderChild} />
    </section>
  );
}

function VariablesPanel({ session }: { session: ChatflowSession }) {
  const { t } = useTranslation("workflows");
  const conversationId = useStore(session.meta, (meta) => meta.conversationId);
  const variables = useConversationVariables(conversationId);
  if (!conversationId) {
    return <EmptyState title={t("chat.varsNoConversation")} hint={t("chat.varsHint")} />;
  }
  let body;
  if (variables.isPending) {
    body = <Spinner label={t("chat.varsLoading")} />;
  } else if (variables.isError) {
    body = (
      <ErrorAlert
        error={variables.error}
        title={t("chat.varsFailed")}
        onRetry={() => void variables.refetch()}
      />
    );
  } else if (variables.data.length === 0) {
    body = <p className="text-sm text-ink-muted">{t("conv.empty")}</p>;
  } else {
    body = (
      <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
        {variables.data.map((item) => (
          <li key={item.name} className="flex flex-col gap-1 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm text-ink">conv.{item.name}</span>
              <Badge>{item.type}</Badge>
            </div>
            <div className="font-mono text-xs break-all text-ink-muted">
              {typeof item.value === "string" ? (
                item.value || t("chat.varsEmptyString")
              ) : (
                <JsonTree value={item.value} />
              )}
            </div>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <section aria-label={t("chat.variables")} className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 text-xs text-ink-muted">{t("chat.varsHint")}</p>
        <Button
          size="iconSm"
          variant="ghost"
          aria-label={t("chat.varsRefresh")}
          title={t("chat.varsRefresh")}
          onClick={() => void variables.refetch()}
        >
          <RefreshCw className="size-3.5" aria-hidden />
        </Button>
      </div>
      {body}
    </section>
  );
}

/*
 * 실행 탭의 챗플로우 미리보기(설계 7.3): 미리보기 대화 고르기·새 대화·이름·지우기, 대화(메시지·승인 카드·
 * 입력 폼·파일·중지), 지금 턴의 노드 타임라인, 대화 변수 현재값. 메시지를 보내기 전에 초안을 저장한다.
 */
export function ChatflowPreview({
  workflow,
  session,
  tab,
  onTabChange,
  current,
  focusId,
  beforeSend,
  onSendError,
}: {
  workflow: WorkflowOut;
  session: ChatflowSession;
  tab: ChatTab;
  onTabChange: (tab: ChatTab) => void;
  current: CurrentRun;
  focusId: string | null;
  beforeSend: () => Promise<true | unknown>;
  onSendError: (error: unknown) => boolean;
}) {
  const { t } = useTranslation("workflows");
  const graph = useFlow((state) => state.graph);
  const webapp = useFlow((state) => state.settings.webapp);
  const fields = useMemo(() => startFieldsOf(graph), [graph]);
  const { titleOf } = useTimelineParts();
  const opening = webapp?.opening_statement ?? null;
  useEffect(() => session.setOpening(opening), [session, opening]);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ConversationBar workflow={workflow} session={session} />
      <Tabs
        value={tab}
        onValueChange={(value) => onTabChange(value as ChatTab)}
        className="flex min-h-0 flex-1 flex-col"
      >
        <TabsList aria-label={t("chat.tabsLabel")} className="shrink-0 px-2">
          {CHAT_TABS.map((key) => (
            <TabsTrigger key={key} value={key}>
              {t(`chat.tabs.${key}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="chat" className="flex min-h-0 flex-1 flex-col">
          <ChatflowChat
            session={session}
            fields={fields}
            upload={(file) => uploadWorkflowFile(workflow.id, file)}
            suggested={webapp?.suggested_questions}
            nodeTitle={titleOf}
            beforeSend={beforeSend}
            onSendError={onSendError}
            toolbar={
              <Badge tone="info" title={t("chat.draftHint")}>
                {t("chat.draftBadge")}
              </Badge>
            }
          />
        </TabsContent>
        <TabsContent value="nodes" className="min-h-0 flex-1 overflow-y-auto p-4">
          <NodesPanel workflow={workflow} current={current} focusId={focusId} />
        </TabsContent>
        <TabsContent value="variables" className="min-h-0 flex-1 overflow-y-auto p-4">
          <VariablesPanel session={session} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
