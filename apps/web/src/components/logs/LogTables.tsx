import { ScrollText } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { useLogRuns, useLogSessions } from "@/features/logs/api";
import { childRunsPath, sessionLogPath, type LogFilters } from "@/features/logs/filters";
import { formatPast } from "@/features/logs/time";
import type { RunLogItem, SessionOut } from "@/features/logs/types";
import { formatCost } from "@/features/settings/usage";
import { formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { RunStatusBadge, SourceBadge } from "./badges";

function TimeCell({ iso, now }: { iso: string | null | undefined; now: number }) {
  return (
    <time dateTime={iso ?? undefined} title={formatDateTime(iso)} className="whitespace-nowrap">
      {formatPast(iso, now)}
    </time>
  );
}

function ListFooter({
  hasMore,
  loading,
  onMore,
}: {
  hasMore: boolean;
  loading: boolean;
  onMore: () => void;
}) {
  const { t } = useTranslation("logs");
  if (!hasMore) return null;
  return (
    <div className="flex justify-center border-t border-line py-2">
      <Button size="sm" variant="ghost" loading={loading} onClick={onMore}>
        {t("list.more")}
      </Button>
    </div>
  );
}

function TableShell({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-line text-left text-xs text-ink-muted">{head}</thead>
        <tbody className="divide-y divide-line">{children}</tbody>
      </table>
    </div>
  );
}

const th = "px-3 py-2 font-medium whitespace-nowrap";
const td = "px-3 py-2 align-top";

function Empty({ filtered, onClear }: { filtered: boolean; onClear: () => void }) {
  const { t } = useTranslation("logs");
  return (
    <EmptyState
      icon={ScrollText}
      title={filtered ? t("list.emptyFiltered") : t("empty")}
      hint={filtered ? undefined : t("list.emptyHint")}
      action={
        filtered ? (
          <Button size="sm" onClick={onClear}>
            {t("filters.clear")}
          </Button>
        ) : null
      }
      className="m-4"
    />
  );
}

export function SessionTable({
  filters,
  agentNames,
  filtered,
  onClearFilters,
}: {
  filters: LogFilters;
  agentNames: Map<string, string>;
  filtered: boolean;
  onClearFilters: () => void;
}) {
  const { t } = useTranslation("logs");
  const now = useNow();
  const query = useLogSessions(filters);
  if (query.isPending) {
    return (
      <div className="flex justify-center p-8">
        <Spinner label={t("list.loading")} />
      </div>
    );
  }
  if (query.isError) {
    return (
      <ErrorAlert
        error={query.error}
        title={t("list.loadFailed")}
        onRetry={() => void query.refetch()}
        className="m-4"
      />
    );
  }
  const items: SessionOut[] = query.data.pages.flatMap((page) => page.items);
  if (items.length === 0) return <Empty filtered={filtered} onClear={onClearFilters} />;
  return (
    <>
      <TableShell
        head={
          <tr>
            <th className={th}>{t("columns.session")}</th>
            <th className={th}>{t("columns.agent")}</th>
            <th className={th}>{t("columns.source")}</th>
            <th className={`${th} text-right`}>{t("columns.messages")}</th>
            <th className={th}>{t("columns.lastActivity")}</th>
            <th className={th}>{t("columns.created")}</th>
          </tr>
        }
      >
        {items.map((session) => (
          <tr key={session.id} className="hover:bg-hover/60">
            <td className={td}>
              <Link
                to={sessionLogPath(session.id)}
                className="font-medium text-ink hover:text-accent hover:underline"
              >
                {session.title || t("untitled")}
              </Link>
              {session.external_user ? (
                <span className="ml-2 text-xs text-ink-muted">
                  {t("externalUser", { user: session.external_user })}
                </span>
              ) : null}
              {session.approval_override === "off" ? (
                <Badge tone="warn" className="ml-2">
                  {t("autoApprove")}
                </Badge>
              ) : null}
            </td>
            <td className={td}>
              {agentNames.get(session.agent_id) ?? (
                <span className="text-ink-subtle">{t("deletedAgent")}</span>
              )}
            </td>
            <td className={td}>
              <SourceBadge source={session.source} />
            </td>
            <td className={`${td} text-right tabular-nums`}>
              {formatNumber(session.message_count)}
            </td>
            <td className={`${td} text-ink-muted`}>
              <TimeCell iso={session.last_message_at ?? session.created_at} now={now} />
            </td>
            <td className={`${td} text-ink-muted`}>
              <TimeCell iso={session.created_at} now={now} />
            </td>
          </tr>
        ))}
      </TableShell>
      <ListFooter
        hasMore={query.hasNextPage}
        loading={query.isFetchingNextPage}
        onMore={() => void query.fetchNextPage()}
      />
    </>
  );
}

function runDuration(run: RunLogItem): number | null {
  if (!run.started_at || !run.ended_at) return null;
  return new Date(run.ended_at).getTime() - new Date(run.started_at).getTime();
}

export function RunTable({
  filters,
  filtered,
  onClearFilters,
  showAgent = true,
}: {
  filters: LogFilters;
  filtered: boolean;
  onClearFilters: () => void;
  showAgent?: boolean;
}) {
  const { t } = useTranslation("logs");
  const now = useNow();
  const query = useLogRuns(filters);
  if (query.isPending) {
    return (
      <div className="flex justify-center p-8">
        <Spinner label={t("list.loading")} />
      </div>
    );
  }
  if (query.isError) {
    return (
      <ErrorAlert
        error={query.error}
        title={t("list.loadFailed")}
        onRetry={() => void query.refetch()}
        className="m-4"
      />
    );
  }
  const items = query.data.pages.flatMap((page) => page.items);
  if (items.length === 0) return <Empty filtered={filtered} onClear={onClearFilters} />;
  return (
    <>
      <TableShell
        head={
          <tr>
            <th className={th}>{t("columns.queued")}</th>
            <th className={th}>{t("columns.session")}</th>
            {showAgent ? <th className={th}>{t("columns.agent")}</th> : null}
            <th className={th}>{t("columns.source")}</th>
            <th className={th}>{t("columns.status")}</th>
            <th className={`${th} text-right`}>{t("columns.steps")}</th>
            <th className={`${th} text-right`}>{t("columns.tokens")}</th>
            <th className={`${th} text-right`}>{t("columns.cost")}</th>
            <th className={`${th} text-right`}>{t("columns.duration")}</th>
          </tr>
        }
      >
        {items.map((run) => (
          <tr key={run.id} className="hover:bg-hover/60">
            <td className={`${td} text-ink-muted`}>
              <TimeCell iso={run.queued_at} now={now} />
            </td>
            <td className={td}>
              <Link
                to={sessionLogPath(run.session_id, run.id)}
                className="font-medium text-ink hover:text-accent hover:underline"
              >
                {run.session_title || t("untitled")}
              </Link>
              {run.session_deleted ? <Badge className="ml-2">{t("sessionDeleted")}</Badge> : null}
              {run.parent_run_id ? (
                <Badge tone="info" className="ml-2" title={t("children.badgeHint")}>
                  {t("children.badge")}
                </Badge>
              ) : null}
              {run.child_count > 0 ? (
                <Link
                  to={childRunsPath(run.id)}
                  title={t("children.countHint", { count: run.child_count })}
                  className="ml-2 inline-flex h-5 items-center rounded-sm border border-line bg-sunken px-1.5 text-xs text-ink-muted hover:text-accent"
                >
                  {t("children.count", { count: run.child_count })}
                </Link>
              ) : null}
            </td>
            {showAgent ? (
              <td className={td}>
                {run.agent_name}
                {run.agent_deleted ? (
                  <span className="ml-1 text-xs text-ink-subtle">{t("deletedMark")}</span>
                ) : null}
              </td>
            ) : null}
            <td className={td}>
              <SourceBadge source={run.session_source} />
            </td>
            <td className={td}>
              <RunStatusBadge status={run.status} />
              {run.child ? (
                <span className="mt-0.5 block text-xs text-ink-muted">
                  {t("children.progress")}
                </span>
              ) : null}
              {run.error ? (
                <span className="mt-0.5 block font-mono text-xs text-danger">{run.error.code}</span>
              ) : null}
            </td>
            <td className={`${td} text-right tabular-nums`}>{formatNumber(run.steps)}</td>
            <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
              {t("tokensPair", {
                input: formatNumber(run.usage.input_tokens),
                output: formatNumber(run.usage.output_tokens),
              })}
              {run.usage.estimated ? (
                <span className="ml-1 text-xs text-ink-subtle">{t("estimated")}</span>
              ) : null}
            </td>
            <td className={`${td} text-right tabular-nums`}>{formatCost(run.usage.cost_usd)}</td>
            <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
              {formatDuration(runDuration(run))}
            </td>
          </tr>
        ))}
      </TableShell>
      <ListFooter
        hasMore={query.hasNextPage}
        loading={query.isFetchingNextPage}
        onMore={() => void query.fetchNextPage()}
      />
    </>
  );
}
