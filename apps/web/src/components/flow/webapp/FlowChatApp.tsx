import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ellipsis, MessageSquarePlus, Pencil, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, useMatch, useNavigate } from "react-router";
import { useStore } from "zustand";
import type { FlowAppOut, WfConversationOut } from "@/api/types";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  createFlowConversation,
  deleteFlowConversation,
  flowKeys,
  renameFlowConversation,
  uploadFlowFile,
  useFlowConversations,
  webChatApi,
  webWfRunApi,
} from "@/features/webapp/flows";
import { ChatflowSession } from "@/features/workflows/chatflowSession";
import "@/features/workflows/i18n";
import { WfRunController } from "@/features/workflows/wfRunController";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { ChatflowChat } from "../chat/ChatflowChat";
import { ConversationTitleDialog } from "../chat/ConversationTitleDialog";

function flowPath(slug: string): string {
  return `/flows/${encodeURIComponent(slug)}`;
}

// 시계는 30초마다 바뀐다. 그 사이에 끝난 대화가 "N초 후"로 보이지 않게 지난 시각으로 다룬다.
function relativeTime(iso: string, now: number): string {
  const at = new Date(iso).getTime();
  return formatRelative(iso, Number.isNaN(at) ? now : Math.max(now, at));
}

function ConversationSidebar({
  app,
  onNew,
  onRename,
  onDelete,
}: {
  app: FlowAppOut;
  onNew: () => void;
  onRename: (conversation: WfConversationOut) => void;
  onDelete: (conversation: WfConversationOut) => void;
}) {
  const { t } = useTranslation("webapp");
  const { t: tw } = useTranslation("workflows");
  const conversations = useFlowConversations(app.slug);
  const now = useNow();
  const items = conversations.data?.pages.flatMap((page) => page.items) ?? [];

  let list;
  if (conversations.isPending) {
    list = <Spinner className="mx-2 my-3" label={t("sessions.label")} />;
  } else if (conversations.isError) {
    list = (
      <ErrorAlert
        error={conversations.error}
        title={t("sessions.loadFailed")}
        onRetry={() => void conversations.refetch()}
      />
    );
  } else if (items.length === 0) {
    list = <p className="px-2 py-3 text-sm text-ink-muted">{t("sessions.empty")}</p>;
  } else {
    list = (
      <ul className="flex flex-col gap-0.5">
        {items.map((item) => {
          const name = item.title || t("sessions.untitled");
          return (
            <li key={item.id} className="relative">
              <NavLink
                to={`${flowPath(app.slug)}/c/${item.id}`}
                className={({ isActive }) =>
                  cn(
                    "block rounded-md py-2 pr-9 pl-2.5 hover:bg-hover",
                    isActive ? "bg-accent-soft text-accent" : "text-ink",
                  )
                }
              >
                <span className="block truncate text-sm">{name}</span>
                <span className="block text-xs text-ink-subtle">
                  {relativeTime(item.last_message_at ?? item.created_at, now)}
                </span>
              </NavLink>
              <Menu>
                <MenuTrigger asChild>
                  <Button
                    size="iconSm"
                    variant="ghost"
                    className="absolute top-2 right-1"
                    aria-label={tw("flowApp.conversationMenu", { name })}
                    title={tw("flowApp.conversationMenu", { name })}
                  >
                    <Ellipsis className="size-4" aria-hidden />
                  </Button>
                </MenuTrigger>
                <MenuContent align="end">
                  <MenuItem onSelect={() => onRename(item)}>
                    <Pencil className="size-3.5" aria-hidden />
                    {tw("chat.rename")}
                  </MenuItem>
                  <MenuItem tone="danger" onSelect={() => onDelete(item)}>
                    <Trash2 className="size-3.5" aria-hidden />
                    {tw("chat.delete")}
                  </MenuItem>
                </MenuContent>
              </Menu>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <aside className="flex min-h-0 flex-col border-r border-line bg-surface">
      <div className="flex items-center gap-2.5 px-4 pt-4 pb-3">
        <AgentIcon icon={app.icon} name={app.name} className="size-8 text-base" />
        <span className="min-w-0 truncate text-sm font-semibold text-ink">{app.name}</span>
      </div>
      <div className="px-3 pb-2">
        <Button size="sm" className="w-full" onClick={onNew}>
          <MessageSquarePlus className="size-4" aria-hidden />
          {t("sessions.new")}
        </Button>
      </div>
      <nav aria-label={t("sessions.label")} className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {list}
        {conversations.hasNextPage ? (
          <Button
            size="sm"
            variant="ghost"
            className="mt-1 w-full"
            loading={conversations.isFetchingNextPage}
            onClick={() => void conversations.fetchNextPage()}
          >
            {t("sessions.more")}
          </Button>
        ) : null}
      </nav>
      <div className="border-t border-line px-4 py-2.5">
        <Link
          to="/workflows"
          className="inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          {t("console")}
        </Link>
      </div>
    </aside>
  );
}

/*
 * 챗플로우 웹앱(설계 7.2 /flows/:slug/c/:cid, 7.4): 대화 목록·새 대화·이름 바꾸기·지우기와 채팅. 1단계
 * 웹앱(/apps/:slug)과 같은 뼈대이고 대화는 주소를 따른다. 새로 고치면 진행 중인 런(입력 대기 포함)에 다시
 * 붙는다. 대화는 만든 때의 발행 버전으로 이어 간다.
 */
export function FlowChatApp({ app }: { app: FlowAppOut }) {
  const { t } = useTranslation("webapp");
  const { t: tw } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const cid = useMatch("/flows/:slug/c/:cid")?.params.cid ?? null;
  const [initialCid] = useState(cid);
  const base = flowPath(app.slug);
  const [controller] = useState(() => new WfRunController(webWfRunApi));
  const [session] = useState(
    () => new ChatflowSession(webChatApi(app.slug, app.workflow_id), controller),
  );
  const detail = useStore(session.meta, (meta) => meta.detail);
  const [renaming, setRenaming] = useState<WfConversationOut | null>(null);
  const [deleting, setDeleting] = useState<WfConversationOut | null>(null);
  const [removing, setRemoving] = useState(false);
  const title = app.webapp.title || app.name;
  const opening = app.webapp.opening_statement ?? null;

  const refreshList = () =>
    queryClient.invalidateQueries({ queryKey: flowKeys.conversations(app.slug) });

  useEffect(() => {
    controller.configure({ onError: (error) => toast.error(errorMessage(error, tc)) });
    session.configure({
      createConversation: () => createFlowConversation(app.slug),
      onConversationChange: (conversationId) => {
        navigate(conversationId ? `${base}/c/${conversationId}` : base, { replace: true });
        void refreshList();
      },
      onSent: () => void refreshList(),
      onSettled: () => void refreshList(),
      onError: (error) => toast.error(errorMessage(error, tc)),
    });
  });
  useEffect(() => session.setOpening(opening), [session, opening]);
  // StrictMode가 효과를 다시 돌려도(정리에서 버린 뒤) 같은 대화를 다시 연다.
  useEffect(() => {
    void session.open(session.conversationId ?? initialCid);
    return () => {
      session.dispose();
      controller.dispose();
    };
  }, [session, controller, initialCid]);
  // 목록에서 다른 대화를 고르거나 새 대화로 가면 주소가 바뀐다. 첫 메시지로 만든 대화는 이미 열려 있다.
  useEffect(() => {
    if (cid !== session.conversationId) void session.open(cid);
  }, [cid, session]);

  const older = cid !== null && detail?.id === cid && detail.wf_version_id !== app.version_id;

  // 웹앱에는 그래프가 없어 승인 카드의 노드 이름을 노드 런에서 찾는다.
  const nodeTitle = (nodeId: string) =>
    Object.values(controller.store.getState().nodeRuns).find((run) => run.nodeId === nodeId)
      ?.title ?? nodeId;

  async function remove() {
    if (!deleting) return;
    setRemoving(true);
    try {
      await deleteFlowConversation(deleting.id);
      if (deleting.id === session.conversationId) session.startNew();
      void refreshList();
      toast.success(tw("chat.deleted"));
      setDeleting(null);
    } catch (error) {
      toast.error(errorMessage(error, tc));
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div className="grid h-full grid-cols-[16rem_minmax(0,1fr)] bg-canvas">
      <ConversationSidebar
        app={app}
        onNew={() => {
          session.startNew();
          navigate(base, { replace: true });
        }}
        onRename={setRenaming}
        onDelete={setDeleting}
      />
      <main aria-label={title} className="flex min-h-0 min-w-0 flex-col">
        <header className="flex items-center gap-3 border-b border-line bg-surface px-5 py-3">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-ink">{title}</h1>
            {app.webapp.description || app.description ? (
              <p className="truncate text-xs text-ink-muted">
                {app.webapp.description || app.description}
              </p>
            ) : null}
          </div>
          {older ? (
            <Badge tone="warn" title={t("olderVersionHint", { version: app.version_number })}>
              {t("olderVersion")}
            </Badge>
          ) : (
            <Badge tone="accent" title={t("versionHint", { version: app.version_number })}>
              {t("version", { version: app.version_number })}
            </Badge>
          )}
        </header>
        <div className="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col">
          <ChatflowChat
            session={session}
            fields={app.fields}
            upload={(file) => uploadFlowFile(app.slug, file)}
            suggested={app.webapp.suggested_questions}
            nodeTitle={nodeTitle}
          />
        </div>
      </main>
      <ConversationTitleDialog
        open={renaming !== null}
        onOpenChange={(open) => {
          if (!open) setRenaming(null);
        }}
        initial={renaming?.title ?? ""}
        onSave={async (next) => {
          if (!renaming) return;
          await renameFlowConversation(renaming.id, next);
          await refreshList();
          setRenaming(null);
          toast.success(tw("chat.renamed"));
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={tw("chat.deleteTitle")}
        description={tw("chat.deleteBody", {
          name: deleting?.title || t("sessions.untitled"),
        })}
        confirmLabel={tc("actions.delete")}
        pending={removing}
        onConfirm={() => void remove()}
      />
    </div>
  );
}
