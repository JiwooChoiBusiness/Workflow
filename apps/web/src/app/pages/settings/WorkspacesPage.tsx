import {
  Ellipsis,
  HardDrive,
  OctagonX,
  PackageX,
  RefreshCw,
  RotateCcw,
  ScrollText,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { RunStatusBadge, SourceBadge } from "@/components/logs/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { EmptyState, PageHeader, Panel } from "@/components/ui/layout";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { sessionLogPath } from "@/features/logs/filters";
import { formatPast } from "@/features/logs/time";
import {
  useCleanWorkspacePackages,
  useKillWorkspaceProcesses,
  useResetWorkspace,
  useWorkspaces,
  type WorkspaceOverview,
} from "@/features/workspaces/api";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useNow } from "@/lib/useNow";

function RowActions({ item }: { item: WorkspaceOverview }) {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const kill = useKillWorkspaceProcesses(item.session_id);
  const clean = useCleanWorkspacePackages(item.session_id);
  const reset = useResetWorkspace(item.session_id);
  const [confirm, setConfirm] = useState<"clean" | "reset" | null>(null);
  const busy = item.status === "busy";
  const fail = (error: unknown) => toast.error(errorMessage(error, tc));

  const runConfirmed = () => {
    const action = confirm === "clean" ? clean : reset;
    action.mutate(undefined, {
      onSuccess: () => {
        toast.success(confirm === "clean" ? t("workspaces.cleaned") : t("workspaces.resetDone"));
        setConfirm(null);
      },
      onError: (error) => {
        fail(error);
        setConfirm(null);
      },
    });
  };

  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <Button
            size="iconSm"
            variant="ghost"
            aria-label={t("workspaces.actions", { title: item.session_title ?? item.session_id })}
          >
            {kill.isPending || clean.isPending || reset.isPending ? (
              <Spinner className="size-3.5" />
            ) : (
              <Ellipsis className="size-4" aria-hidden />
            )}
          </Button>
        </MenuTrigger>
        <MenuContent align="end">
          <MenuItem
            disabled={(item.running ?? []).length === 0}
            onSelect={() =>
              kill.mutate(null, {
                onSuccess: (result) =>
                  toast.success(t("workspaces.killed", { count: result.killed.length })),
                onError: fail,
              })
            }
          >
            <OctagonX className="size-4" aria-hidden />
            {t("workspaces.kill")}
          </MenuItem>
          <MenuItem disabled={busy} onSelect={() => setConfirm("clean")}>
            <PackageX className="size-4" aria-hidden />
            {t("workspaces.cleanPackages")}
          </MenuItem>
          <MenuItem disabled={busy} tone="danger" onSelect={() => setConfirm("reset")}>
            <RotateCcw className="size-4" aria-hidden />
            {t("workspaces.reset")}
          </MenuItem>
          <MenuSeparator />
          <MenuItem onSelect={() => navigate(sessionLogPath(item.session_id))}>
            <ScrollText className="size-4" aria-hidden />
            {t("workspaces.openSession")}
          </MenuItem>
        </MenuContent>
      </Menu>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={confirm === "reset" ? t("workspaces.resetTitle") : t("workspaces.cleanTitle")}
        description={
          confirm === "reset"
            ? t("workspaces.resetDescription", { title: item.session_title ?? item.session_id })
            : t("workspaces.cleanDescription", { title: item.session_title ?? item.session_id })
        }
        confirmLabel={confirm === "reset" ? t("workspaces.reset") : t("workspaces.cleanPackages")}
        tone={confirm === "reset" ? "danger" : "default"}
        pending={clean.isPending || reset.isPending}
        onConfirm={runConfirmed}
      />
    </>
  );
}

function WorkspaceRow({ item, now }: { item: WorkspaceOverview; now: number }) {
  const { t } = useTranslation("settings");
  const td = "px-3 py-2 align-top";
  return (
    <tr className="hover:bg-hover/60">
      <td className={td}>
        <Link
          to={sessionLogPath(item.session_id)}
          className="font-medium text-ink hover:text-accent hover:underline"
        >
          {item.session_title || t("workspaces.untitled")}
        </Link>
        <span className="mt-0.5 flex items-center gap-1.5">
          {item.session_source ? <SourceBadge source={item.session_source} /> : null}
          <span className="font-mono text-xs text-ink-subtle">{item.session_id.slice(0, 8)}</span>
        </span>
      </td>
      <td className={td}>
        {item.agent_name ?? "-"}
        {item.agent_deleted ? (
          <span className="ml-1 text-xs text-ink-subtle">{t("workspaces.deletedMark")}</span>
        ) : null}
      </td>
      <td className={td}>
        <span className="flex flex-col items-start gap-1">
          <Badge tone={item.status === "busy" ? "info" : "neutral"}>
            {t(`workspaces.status.${item.status}`)}
          </Badge>
          {item.active_run_status ? <RunStatusBadge status={item.active_run_status} /> : null}
        </span>
      </td>
      <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
        <span title={formatDateTime(item.disk_measured_at)}>
          {formatBytes(item.disk_bytes)}
          {item.disk_estimated ? <span className="text-ink-subtle">+</span> : null}
        </span>
        {item.over_limit ? (
          <Badge tone="danger" className="ml-1.5">
            {t("workspaces.overLimit")}
          </Badge>
        ) : null}
        <span className="block text-xs text-ink-subtle">
          {item.disk_measured_at
            ? formatPast(item.disk_measured_at, now)
            : t("workspaces.notMeasured")}
        </span>
      </td>
      <td className={`${td} text-right whitespace-nowrap tabular-nums`}>
        {(item.running ?? []).length > 0 ? (
          <>
            <span>{t("workspaces.processCount", { count: (item.running ?? []).length })}</span>
            <span className="block text-xs text-ink-muted" title={t("workspaces.peakHint")}>
              {t("workspaces.peak", { size: formatBytes(item.peak_memory_bytes) })}
            </span>
          </>
        ) : (
          <span className="text-ink-subtle">-</span>
        )}
      </td>
      <td className={`${td} whitespace-nowrap text-ink-muted`}>
        <time dateTime={item.last_activity_at} title={formatDateTime(item.last_activity_at)}>
          {formatPast(item.last_activity_at, now)}
        </time>
      </td>
      <td className={`${td} text-right`}>
        <RowActions item={item} />
      </td>
    </tr>
  );
}

// 설정 > 작업공간(설계 7.3 WorkspaceTable): 세션 작업공간 목록과 정리.
export default function WorkspacesPage() {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const now = useNow();
  const list = useWorkspaces();

  if (list.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (list.isError) {
    return (
      <ErrorAlert
        error={list.error}
        title={t("workspaces.loadFailed")}
        onRetry={() => void list.refetch()}
      />
    );
  }
  const data = list.data;
  const th = "px-3 py-2 font-medium whitespace-nowrap";
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t("workspaces.title")}
        description={t("workspaces.description")}
        className="mb-0"
        actions={
          <Button size="sm" loading={list.isFetching} onClick={() => void list.refetch()}>
            <RefreshCw className="size-3.5" aria-hidden />
            {t("workspaces.refresh")}
          </Button>
        }
      />
      <p className="text-sm text-ink-muted">
        {t("workspaces.summary", {
          count: data.items.length,
          total: formatBytes(data.total_disk_bytes),
          limit: formatBytes(data.disk_limit_bytes),
        })}
      </p>
      {data.items.length === 0 ? (
        <EmptyState
          icon={HardDrive}
          title={t("workspaces.empty")}
          hint={t("workspaces.emptyHint")}
        />
      ) : (
        <Panel bodyClassName="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-line text-left text-xs text-ink-muted">
                <tr>
                  <th className={th}>{t("workspaces.column.session")}</th>
                  <th className={th}>{t("workspaces.column.agent")}</th>
                  <th className={th}>{t("workspaces.column.state")}</th>
                  <th className={`${th} text-right`}>{t("workspaces.column.disk")}</th>
                  <th className={`${th} text-right`}>{t("workspaces.column.processes")}</th>
                  <th className={th}>{t("workspaces.column.lastActivity")}</th>
                  <th className={th}>
                    <span className="sr-only">{t("workspaces.column.actions")}</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.items.map((item) => (
                  <WorkspaceRow key={item.id} item={item} now={now} />
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <p className="text-xs text-ink-muted">{t("workspaces.footnote")}</p>
    </div>
  );
}
