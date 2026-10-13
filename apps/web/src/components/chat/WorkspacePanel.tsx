import { HardDrive, OctagonX, PackageX, RefreshCw, RotateCcw } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { ConfirmDialog, Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { KeyValueList, Panel } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  useCleanWorkspacePackages,
  useKillWorkspaceProcesses,
  useResetWorkspace,
  useSessionWorkspace,
  type WorkspaceStatusOut,
} from "@/features/workspaces/api";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";

function DiskBar({ used, limit }: { used: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  return (
    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-sunken" aria-hidden>
      <div
        className={cn(
          "h-full rounded-full",
          ratio >= 1 ? "bg-danger" : ratio >= 0.8 ? "bg-warn" : "bg-accent",
        )}
        style={{ width: `${Math.max(ratio * 100, used > 0 ? 2 : 0)}%` }}
      />
    </div>
  );
}

function RunningTable({
  status,
  onKill,
  killing,
}: {
  status: WorkspaceStatusOut;
  onKill: (execId: string) => void;
  killing: boolean;
}) {
  const { t } = useTranslation("chat");
  if ((status.running ?? []).length === 0) {
    return <p className="text-xs text-ink-muted">{t("workspace.noProcesses")}</p>;
  }
  return (
    <table className="w-full text-xs">
      <thead className="text-left text-ink-muted">
        <tr>
          <th className="pb-1 font-medium">{t("workspace.processKind")}</th>
          <th className="pb-1 font-medium">PID</th>
          <th className="pb-1 text-right font-medium">{t("workspace.elapsed")}</th>
          <th className="pb-1 text-right font-medium" title={t("workspace.peakHint")}>
            {t("workspace.peak")}
          </th>
          <th className="pb-1" />
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {(status.running ?? []).map((item) => (
          <tr key={item.exec_id}>
            <td className="py-1">
              <span className="font-mono">{item.kind}</span>
              {item.active_processes && item.active_processes > 1 ? (
                <span className="ml-1 text-ink-subtle">
                  {t("workspace.children", { count: item.active_processes })}
                </span>
              ) : null}
            </td>
            <td className="py-1 font-mono tabular-nums">{item.pid}</td>
            <td className="py-1 text-right tabular-nums">{formatDuration(item.elapsed_ms)}</td>
            <td className="py-1 text-right tabular-nums">{formatBytes(item.peak_memory_bytes)}</td>
            <td className="py-1 text-right">
              <Button
                size="sm"
                variant="dangerGhost"
                disabled={killing}
                onClick={() => onKill(item.exec_id)}
                aria-label={t("workspace.killOne", { pid: item.pid })}
              >
                <OctagonX className="size-3.5" aria-hidden />
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function WorkspaceBody({ sessionId, status }: { sessionId: string; status: WorkspaceStatusOut }) {
  const { t } = useTranslation("chat");
  const { t: tc } = useTranslation();
  const kill = useKillWorkspaceProcesses(sessionId);
  const clean = useCleanWorkspacePackages(sessionId);
  const reset = useResetWorkspace(sessionId);
  const [confirm, setConfirm] = useState<"clean" | "reset" | null>(null);
  const busy = status.instance?.status === "busy";
  const disk = status.disk_bytes ?? status.instance?.disk_bytes ?? null;
  const limit = status.disk_limit_bytes;
  const overLimit = disk !== null && limit > 0 && disk > limit;

  const killProcesses = (execId: string | null) =>
    kill.mutate(execId, {
      onSuccess: (result) =>
        toast.success(
          result.killed.length > 0
            ? t("workspace.killed", { count: result.killed.length })
            : t("workspace.nothingToKill"),
        ),
      onError: (error) => toast.error(errorMessage(error, tc)),
    });
  const runConfirmed = () => {
    const action = confirm === "clean" ? clean : reset;
    action.mutate(undefined, {
      onSuccess: () => {
        toast.success(confirm === "clean" ? t("workspace.cleaned") : t("workspace.resetDone"));
        setConfirm(null);
      },
      onError: (error) => {
        toast.error(errorMessage(error, tc));
        setConfirm(null);
      },
    });
  };

  const items: { label: ReactNode; value: ReactNode }[] = [
    {
      label: t("workspace.state"),
      value: !status.exists ? (
        <span className="text-ink-muted">{t("workspace.missing")}</span>
      ) : (
        <Badge tone={busy ? "info" : "ok"}>
          {busy ? t("workspace.busy") : t("workspace.ready")}
        </Badge>
      ),
    },
    {
      label: t("workspace.path"),
      value: status.instance ? (
        <span className="flex min-w-0 items-center gap-1">
          <span className="min-w-0 font-mono text-xs break-all">{status.instance.root_path}</span>
          <CopyButton value={status.instance.root_path} size="iconSm" variant="ghost" />
        </span>
      ) : (
        "-"
      ),
    },
    {
      label: t("workspace.disk"),
      value: (
        <div>
          <span className="tabular-nums">
            {t("workspace.diskOf", { used: formatBytes(disk), limit: formatBytes(limit) })}
          </span>
          {status.disk_estimated ? (
            <span className="ml-1 text-xs text-ink-subtle">{t("workspace.estimated")}</span>
          ) : null}
          {disk !== null && limit > 0 ? <DiskBar used={disk} limit={limit} /> : null}
        </div>
      ),
    },
    {
      label: t("workspace.lastExec"),
      value: formatDateTime(status.last_exec_at ?? status.instance?.last_activity_at),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      {overLimit ? (
        <Alert tone="danger" title={t("workspace.overLimit")}>
          {t("workspace.overLimitHint")}
        </Alert>
      ) : null}
      <KeyValueList items={items} />
      <section>
        <h3 className="mb-1.5 text-xs font-medium text-ink-muted">{t("workspace.processes")}</h3>
        <RunningTable
          status={status}
          killing={kill.isPending}
          onKill={(execId) => killProcesses(execId)}
        />
      </section>
      {(status.largest_dirs ?? []).length > 0 ? (
        <section>
          <h3 className="mb-1.5 text-xs font-medium text-ink-muted">{t("workspace.largest")}</h3>
          <ul className="flex flex-col gap-0.5 text-xs">
            {(status.largest_dirs ?? []).map((entry) => (
              <li key={entry.path} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate font-mono text-ink">{entry.path}</span>
                <span className="shrink-0 text-ink-muted tabular-nums">
                  {formatBytes(entry.bytes)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={(status.running ?? []).length === 0}
          loading={kill.isPending}
          onClick={() => killProcesses(null)}
        >
          <OctagonX className="size-3.5" aria-hidden />
          {t("workspace.kill")}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          disabled={!status.exists || busy}
          title={busy ? t("workspace.busyHint") : undefined}
          onClick={() => setConfirm("clean")}
        >
          <PackageX className="size-3.5" aria-hidden />
          {t("workspace.cleanPackages")}
        </Button>
        <Button
          size="sm"
          variant="dangerGhost"
          disabled={busy}
          title={busy ? t("workspace.busyHint") : undefined}
          onClick={() => setConfirm("reset")}
        >
          <RotateCcw className="size-3.5" aria-hidden />
          {t("workspace.reset")}
        </Button>
      </div>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={confirm === "reset" ? t("workspace.resetTitle") : t("workspace.cleanTitle")}
        description={
          confirm === "reset" ? t("workspace.resetDescription") : t("workspace.cleanDescription")
        }
        confirmLabel={confirm === "reset" ? t("workspace.reset") : t("workspace.cleanPackages")}
        tone={confirm === "reset" ? "danger" : "default"}
        pending={clean.isPending || reset.isPending}
        onConfirm={runConfirmed}
      />
    </div>
  );
}

function WorkspaceContent({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("chat");
  const status = useSessionWorkspace(sessionId);
  if (sessionId === null) {
    return <p className="text-sm text-ink-muted">{t("workspace.noSession")}</p>;
  }
  if (status.isPending) return <Spinner label={t("workspace.loading")} />;
  if (status.isError) {
    return (
      <ErrorAlert
        error={status.error}
        title={t("workspace.loadFailed")}
        onRetry={() => void status.refetch()}
      />
    );
  }
  return <WorkspaceBody sessionId={sessionId} status={status.data} />;
}

function RefreshButton({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("chat");
  const status = useSessionWorkspace(sessionId);
  return (
    <Button
      size="iconSm"
      variant="ghost"
      aria-label={t("workspace.refresh")}
      title={t("workspace.refresh")}
      disabled={sessionId === null}
      onClick={() => void status.refetch()}
    >
      {status.isFetching ? <Spinner className="size-3.5" /> : <RefreshCw className="size-3.5" />}
    </Button>
  );
}

// 작업공간 패널(설계 3.14, 7.3): 상태·크기·큰 폴더·실행 중 프로세스와 메모리 최고 요청치, 프로세스 종료·
// 패키지 삭제·초기화. 세션 상세에 놓이고, 빌더 미리보기에서는 WorkspaceDialogButton으로 연다.
export function WorkspacePanel({
  sessionId = null,
  className,
}: {
  // 세션이 아직 없으면(미리보기를 열기 전) 안내만 보인다.
  sessionId?: string | null;
  className?: string;
}) {
  const { t } = useTranslation("chat");
  return (
    <Panel
      className={className}
      title={
        <span className="flex items-center gap-1.5">
          <HardDrive className="size-4 text-ink-muted" aria-hidden />
          {t("workspace.title")}
        </span>
      }
      actions={<RefreshButton sessionId={sessionId} />}
    >
      <WorkspaceContent sessionId={sessionId} />
    </Panel>
  );
}

export function WorkspaceDialogButton({
  sessionId,
  size = "iconSm",
}: {
  sessionId: string | null;
  size?: "iconSm" | "sm";
}) {
  const { t } = useTranslation("chat");
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size={size}
          disabled={sessionId === null}
          aria-label={size === "iconSm" ? t("workspace.open") : undefined}
          title={t("workspace.open")}
        >
          <HardDrive className="size-4" aria-hidden />
          {size === "sm" ? t("workspace.title") : null}
        </Button>
      </DialogTrigger>
      <DialogContent title={t("workspace.title")} className="w-[min(36rem,calc(100vw-2rem))]">
        {open ? <WorkspaceContent sessionId={sessionId} /> : null}
      </DialogContent>
    </Dialog>
  );
}
