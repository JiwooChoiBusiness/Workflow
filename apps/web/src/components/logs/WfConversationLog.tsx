import { Filter, MessagesSquare, Paperclip } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { WfConversationOut, WfConversationSource } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Button } from "@/components/ui/button";
import { EmptyState, Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { formatPast } from "@/features/logs/time";
import {
  useConversationMessages,
  useWorkflowConversations,
  wfConversationFileUrl,
} from "@/features/workflows/chatApi";
import "@/features/workflows/i18n";
import { shortRunId, wfRunPath } from "@/features/workflows/runLogFilters";
import { cn } from "@/lib/cn";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { RunStatusBadge, SourceBadge } from "./badges";

const ALL = "__all__";
export const CONVERSATION_SOURCES: readonly WfConversationSource[] = ["preview", "webapp", "api"];

function Messages({
  workflowId,
  conversation,
  onRuns,
}: {
  workflowId: string;
  conversation: WfConversationOut;
  onRuns: () => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation("chat");
  const messages = useConversationMessages(conversation.id);
  let body;
  if (messages.isPending) {
    body = <Spinner label={t("logs.conversations.loading")} />;
  } else if (messages.isError) {
    body = (
      <ErrorAlert
        error={messages.error}
        title={t("logs.conversations.loadFailed")}
        onRetry={() => void messages.refetch()}
      />
    );
  } else {
    body = (
      <ol className="flex flex-col gap-3">
        {messages.data.has_more ? (
          <li className="text-xs text-ink-subtle">{t("logs.conversations.truncated")}</li>
        ) : null}
        {messages.data.items.map((message) => (
          <li
            key={message.id}
            className={cn(
              "flex flex-col gap-1 rounded-md px-3 py-2 text-sm",
              message.role === "user" ? "ml-8 bg-accent-soft" : "mr-8 border border-line",
            )}
          >
            <div className="flex items-center gap-2 text-xs text-ink-muted">
              <span className="font-medium text-ink">
                {message.role === "user" ? tc("role.user") : t("logs.conversations.answer")}
              </span>
              <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
              {message.role === "assistant" && message.status ? (
                <RunStatusBadge status={message.status} />
              ) : null}
              {message.role === "assistant" && message.run_id ? (
                <Link
                  to={wfRunPath(workflowId, message.run_id)}
                  className="ml-auto font-mono text-accent hover:underline"
                >
                  {t("logs.conversations.run", { id: shortRunId(message.run_id) })}
                </Link>
              ) : null}
            </div>
            <p className="break-words whitespace-pre-wrap text-ink">
              {message.text ||
                (message.role === "assistant" ? t("logs.conversations.emptyAnswer") : "")}
            </p>
            {message.files.length > 0 ? (
              <ul className="flex flex-col gap-0.5">
                {message.files.map((file) => (
                  <li key={file.id} className="flex items-center gap-1.5 text-xs text-ink-muted">
                    <Paperclip className="size-3.5 shrink-0" aria-hidden />
                    <a
                      href={wfConversationFileUrl(conversation.id, file.id)}
                      download
                      className="truncate text-accent hover:underline"
                      aria-label={tc("attachments.download", { name: file.filename })}
                    >
                      {file.filename}
                    </a>
                    <span className="tabular-nums">{formatBytes(file.size_bytes)}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ol>
    );
  }
  return (
    <Panel
      title={conversation.title || t("chat.untitled")}
      description={t("logs.conversations.detail", {
        created: formatDateTime(conversation.created_at),
        turns: Math.floor(conversation.message_count / 2),
      })}
      actions={
        <Button size="sm" onClick={onRuns}>
          <Filter className="size-3.5" aria-hidden />
          {t("logs.conversations.runs")}
        </Button>
      }
    >
      {body}
    </Panel>
  );
}

/*
 * 실행 기록 화면의 대화 탭(챗플로우). 출처·제목·턴 수·마지막 시각을 보이고, 고른 대화의 메시지와 턴마다의
 * 런 상세 링크를 옆에 둔다. 고른 대화는 주소의 ?cid=이다.
 */
export function WfConversationLog({
  workflowId,
  selected,
  source,
  onSelect,
  onSourceChange,
  onRuns,
}: {
  workflowId: string;
  selected: string | null;
  source: WfConversationSource | null;
  onSelect: (conversationId: string) => void;
  onSourceChange: (source: WfConversationSource | null) => void;
  onRuns: (conversationId: string) => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tl } = useTranslation("logs");
  const now = useNow();
  const conversations = useWorkflowConversations(workflowId, source);
  const items = conversations.data?.pages.flatMap((page) => page.items) ?? [];
  const current = items.find((item) => item.id === selected) ?? null;

  let list;
  if (conversations.isPending) {
    list = (
      <div className="flex justify-center p-8">
        <Spinner label={tl("list.loading")} />
      </div>
    );
  } else if (conversations.isError) {
    list = (
      <ErrorAlert
        error={conversations.error}
        title={tl("list.loadFailed")}
        onRetry={() => void conversations.refetch()}
        className="m-4"
      />
    );
  } else if (items.length === 0) {
    list = (
      <EmptyState
        icon={MessagesSquare}
        title={t("logs.conversations.empty")}
        hint={source ? undefined : t("logs.conversations.emptyHint")}
        className="m-4"
      />
    );
  } else {
    list = (
      <table className="w-full text-sm">
        <thead className="border-b border-line text-left text-xs text-ink-muted">
          <tr>
            <th className="px-3 py-2 font-medium">{t("logs.conversations.source")}</th>
            <th className="px-3 py-2 font-medium">{t("logs.conversations.title")}</th>
            <th className="px-3 py-2 text-right font-medium">{t("logs.conversations.turns")}</th>
            <th className="px-3 py-2 font-medium whitespace-nowrap">
              {t("logs.conversations.last")}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {items.map((item) => (
            <tr
              key={item.id}
              aria-selected={item.id === selected}
              className={cn("hover:bg-hover/60", item.id === selected && "bg-accent-soft")}
            >
              <td className="px-3 py-2 align-top">
                <SourceBadge source={item.source} />
              </td>
              <td className="max-w-0 px-3 py-2 align-top">
                <button
                  type="button"
                  className="block w-full truncate text-left text-ink hover:text-accent hover:underline"
                  onClick={() => onSelect(item.id)}
                >
                  {item.title || t("chat.untitled")}
                </button>
                {item.external_user ? (
                  <span className="block truncate text-xs text-ink-subtle">
                    {t("logs.conversations.user", { user: item.external_user })}
                  </span>
                ) : null}
              </td>
              <td className="px-3 py-2 text-right align-top tabular-nums">
                {Math.floor(item.message_count / 2)}
              </td>
              <td className="px-3 py-2 align-top whitespace-nowrap text-ink-muted">
                <time
                  dateTime={item.last_message_at ?? item.created_at}
                  title={formatDateTime(item.last_message_at ?? item.created_at)}
                >
                  {formatPast(item.last_message_at ?? item.created_at, now)}
                </time>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          aria-label={t("logs.conversations.source")}
          className="w-48"
          value={source ?? ALL}
          onValueChange={(value) =>
            onSourceChange(value === ALL ? null : (value as WfConversationSource))
          }
          options={[
            { value: ALL, label: tl("filters.all") },
            ...CONVERSATION_SOURCES.map((item) => ({
              value: item,
              label: tl(`sources.${item}`),
            })),
          ]}
          renderValue={(option) => (
            <span>
              <span className="text-ink-muted">{t("logs.conversations.source")}: </span>
              {option?.label ?? tl("filters.all")}
            </span>
          )}
        />
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel bodyClassName="p-0">
          {list}
          {conversations.hasNextPage ? (
            <div className="flex justify-center border-t border-line py-2">
              <Button
                size="sm"
                variant="ghost"
                loading={conversations.isFetchingNextPage}
                onClick={() => void conversations.fetchNextPage()}
              >
                {tl("list.more")}
              </Button>
            </div>
          ) : null}
        </Panel>
        {current ? (
          <Messages
            key={current.id}
            workflowId={workflowId}
            conversation={current}
            onRuns={() => onRuns(current.id)}
          />
        ) : items.length > 0 ? (
          <EmptyState title={t("logs.conversations.pick")} />
        ) : null}
      </div>
    </div>
  );
}
