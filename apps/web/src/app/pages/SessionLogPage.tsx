import { ArrowLeft, Microscope } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router";
import { isApiError } from "@/api/http";
import { WorkspacePanel } from "@/components/chat/WorkspacePanel";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { SourceBadge } from "@/components/logs/badges";
import { MessageTimeline } from "@/components/logs/MessageTimeline";
import { RunInspector } from "@/components/logs/RunInspector";
import { SessionHeader } from "@/components/logs/SessionHeader";
import { SessionRunList } from "@/components/logs/SessionRunList";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useLogSession, useSessionRuns } from "@/features/logs/api";

// 세션 상세(설계 7.2 /logs/sessions/:sid, 7.3): 메시지 타임라인, 런 목록과 인스펙터, 작업공간 패널.
// 고른 런과 펼친 도구 호출은 주소(?run=&tool=)에 둔다.
export default function SessionLogPage() {
  const { t } = useTranslation("logs");
  const { sid = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const session = useLogSession(sid);
  const runs = useSessionRuns(sid);
  const deleted = isApiError(session.error, 404);

  const runList = runs.data ?? [];
  const requestedRun = searchParams.get("run");
  const selectedRunId =
    requestedRun && runList.some((run) => run.id === requestedRun)
      ? requestedRun
      : (runList[0]?.id ?? null);
  const focusToolCallId = searchParams.get("tool");

  const select = (runId: string, toolCallId?: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("run", runId);
    if (toolCallId) next.set("tool", toolCallId);
    else next.delete("tool");
    setSearchParams(next, { replace: true });
  };

  if (session.isPending) return <PageSpinner label={t("session.loading")} />;
  if (session.isError && !deleted) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <ErrorAlert
          error={session.error}
          title={t("session.loadFailed")}
          onRetry={() => void session.refetch()}
        />
      </div>
    );
  }
  const detail = session.data ?? null;
  const title = detail?.title || runList[0]?.session_title || t("untitled");

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-4 px-6 py-6">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="sm" variant="ghost">
          <Link to="/logs">
            <ArrowLeft className="size-4" aria-hidden />
            {t("session.back")}
          </Link>
        </Button>
        <h1 className="min-w-0 truncate text-lg font-semibold text-ink">{title}</h1>
        {detail ? <SourceBadge source={detail.source} /> : null}
      </div>
      {deleted ? (
        <Alert tone="warn" title={t("session.deleted")}>
          {t("session.deletedHint")}
        </Alert>
      ) : null}
      {detail ? <SessionHeader session={detail} /> : null}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        {detail ? (
          <Panel title={t("session.timeline")} bodyClassName="max-h-[80vh] overflow-y-auto">
            <MessageTimeline
              sessionId={sid}
              selectedRunId={selectedRunId}
              onSelectRun={(runId) => select(runId)}
              onOpenTool={(runId, toolCallId) => select(runId, toolCallId)}
            />
          </Panel>
        ) : null}
        <div className="flex min-w-0 flex-col gap-4">
          <Panel title={t("session.runs")}>
            <SessionRunList
              runs={runList}
              loading={runs.isPending}
              error={runs.error}
              onRetry={() => void runs.refetch()}
              selectedRunId={selectedRunId}
              onSelect={(runId) => select(runId)}
            />
          </Panel>
          <Panel title={t("session.inspector")}>
            {selectedRunId ? (
              <RunInspector
                key={selectedRunId}
                runId={selectedRunId}
                sessionId={sid}
                focusToolCallId={focusToolCallId}
              />
            ) : (
              <EmptyState icon={Microscope} title={t("session.inspectorHint")} />
            )}
          </Panel>
          {detail ? <WorkspacePanel sessionId={sid} /> : null}
        </div>
      </div>
    </div>
  );
}
