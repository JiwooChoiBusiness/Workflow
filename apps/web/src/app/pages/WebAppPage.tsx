import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MessageSquarePlus, SearchX } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, useMatch, useNavigate, useParams } from "react-router";
import { isApiError } from "@/api/http";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { useChatSession } from "@/features/sessions/useChatSession";
import {
  createWebAppSession,
  useWebApp,
  useWebAppSessions,
  webappKeys,
  webSessionApi,
  webSessionFileUrl,
  type WebAppOut,
} from "@/features/webapp/api";
import { cn } from "@/lib/cn";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { RunStoreProvider } from "@/stores/runStoreContext";

function appPath(slug: string): string {
  return `/apps/${encodeURIComponent(slug)}`;
}

// 시계는 30초마다 바뀐다. 그 사이에 끝난 대화가 "N초 후"로 보이지 않게 지난 시각으로 다룬다.
function relativeTime(iso: string, now: number): string {
  const at = new Date(iso).getTime();
  return formatRelative(iso, Number.isNaN(at) ? now : Math.max(now, at));
}

function SessionSidebar({ app, onNew }: { app: WebAppOut; onNew: () => void }) {
  const { t } = useTranslation("webapp");
  const sessions = useWebAppSessions(app.slug);
  const now = useNow();
  const items = sessions.data?.pages.flatMap((page) => page.items) ?? [];

  let list;
  if (sessions.isPending) {
    list = <Spinner className="mx-2 my-3" label={t("sessions.label")} />;
  } else if (sessions.isError) {
    list = (
      <ErrorAlert
        error={sessions.error}
        title={t("sessions.loadFailed")}
        onRetry={() => void sessions.refetch()}
      />
    );
  } else if (items.length === 0) {
    list = <p className="px-2 py-3 text-sm text-ink-muted">{t("sessions.empty")}</p>;
  } else {
    list = (
      <ul className="flex flex-col gap-0.5">
        {items.map((item) => (
          <li key={item.id}>
            <NavLink
              to={`${appPath(app.slug)}/s/${item.id}`}
              className={({ isActive }) =>
                cn(
                  "block rounded-md px-2.5 py-2 hover:bg-hover",
                  isActive ? "bg-accent-soft text-accent" : "text-ink",
                )
              }
            >
              <span className="block truncate text-sm">{item.title || t("sessions.untitled")}</span>
              <span className="block text-xs text-ink-subtle">
                {relativeTime(item.last_message_at ?? item.created_at, now)}
              </span>
            </NavLink>
          </li>
        ))}
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
        {sessions.hasNextPage ? (
          <Button
            size="sm"
            variant="ghost"
            className="mt-1 w-full"
            loading={sessions.isFetchingNextPage}
            onClick={() => void sessions.fetchNextPage()}
          >
            {t("sessions.more")}
          </Button>
        ) : null}
      </nav>
      <div className="border-t border-line px-4 py-2.5">
        <Link
          to="/agents"
          className="inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          {t("console")}
        </Link>
      </div>
    </aside>
  );
}

// 웹앱 한 개(설계 7.2·7.3). 대화 목록과 채팅 패널이고, 세션은 주소(/apps/:slug/s/:sid)를 따른다.
function WebApp({ app }: { app: WebAppOut }) {
  const { t } = useTranslation("webapp");
  const { t: ta } = useTranslation("agents");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const sid = useMatch("/apps/:slug/s/:sid")?.params.sid ?? null;
  const base = appPath(app.slug);
  // 열어 본 세션이 고정된 발행 버전. 다시 발행한 뒤의 예전 대화는 그 버전으로 이어 간다.
  const [sessionVersion, setSessionVersion] = useState<{ id: string; versionId: string } | null>(
    null,
  );
  const [api] = useState(() =>
    webSessionApi(app.agent_id, (detail) =>
      setSessionVersion({ id: detail.id, versionId: detail.agent_version_id }),
    ),
  );
  const [childApi] = useState(() => webSessionApi(null));
  const refreshSessions = () =>
    void queryClient.invalidateQueries({ queryKey: webappKeys.sessions(app.slug) });
  const chat = useChatSession({
    initialSessionId: sid,
    api,
    createSession: () => createWebAppSession(app.slug),
    onSessionChange: (sessionId) => {
      navigate(sessionId ? `${base}/s/${sessionId}` : base, { replace: true });
      refreshSessions();
    },
    onSettled: refreshSessions,
  });
  const { controller } = chat;

  // 목록에서 다른 대화를 고르거나 새 대화로 가면 주소가 바뀐다. 첫 메시지로 만든 세션은 이미 열려 있다.
  useEffect(() => {
    if (sid !== controller.sessionId) void controller.open(sid);
  }, [sid, controller]);

  const older =
    sid !== null && sessionVersion?.id === sid && sessionVersion.versionId !== app.version_id;

  return (
    <div className="grid h-full grid-cols-[16rem_minmax(0,1fr)] bg-canvas">
      <SessionSidebar app={app} onNew={() => navigate(base)} />
      <main aria-label={app.name} className="flex min-h-0 min-w-0 flex-col">
        <header className="flex items-center gap-3 border-b border-line bg-surface px-5 py-3">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-ink">{app.name}</h1>
            {app.description ? (
              <p className="truncate text-xs text-ink-muted">{app.description}</p>
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
          <Badge tone={app.approval_mode === "off" ? "danger" : "neutral"}>
            {ta(`approval.chip.${app.approval_mode}`)}
          </Badge>
        </header>
        <div className="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col">
          <RunStoreProvider store={chat.store}>
            <ChatPanel
              className="min-h-0 flex-1"
              sessionId={chat.sessionId}
              fileUrl={webSessionFileUrl}
              sessionApi={childApi}
              attachments
              loading={chat.loading}
              loadError={chat.loadError}
              onRetryLoad={() => void controller.resync()}
              connection={chat.connection}
              sending={chat.sending}
              stopping={chat.stopping}
              onSend={(text, files) => controller.send(text, files)}
              onStop={() => void controller.stop()}
              onRespond={(runId, input) => controller.respond(runId, input)}
              onDisableAutoApprove={() => controller.setAutoApprove(false)}
            />
          </RunStoreProvider>
        </div>
      </main>
    </div>
  );
}

export default function WebAppPage() {
  const { t } = useTranslation("webapp");
  const { slug = "" } = useParams();
  const app = useWebApp(slug);
  if (app.isPending) return <PageSpinner label={t("loading")} />;
  if (app.isError) {
    if (isApiError(app.error, 404)) {
      return (
        <div className="mx-auto max-w-xl p-8">
          <EmptyState
            icon={SearchX}
            title={t("notFoundTitle")}
            hint={t("notFoundHint")}
            action={
              <Button asChild variant="primary">
                <Link to="/agents">{t("console")}</Link>
              </Button>
            }
          />
        </div>
      );
    }
    return (
      <div className="mx-auto max-w-xl p-8">
        <ErrorAlert error={app.error} title={t("loadFailed")} onRetry={() => void app.refetch()} />
      </div>
    );
  }
  // 다른 웹앱으로 옮겨 가면 채팅 상태를 새로 만든다.
  return <WebApp key={app.data.agent_id} app={app.data} />;
}
