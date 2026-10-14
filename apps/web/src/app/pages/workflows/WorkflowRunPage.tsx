import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Square, Workflow } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { WfRunDetail, WorkflowVersionOut } from "@/api/types";
import { ConnectionNotice } from "@/components/chat/MessageList";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { JsonTree } from "@/components/common/JsonTree";
import { RunStatusBadge } from "@/components/logs/badges";
import { EventTable } from "@/components/logs/EventTable";
import { RunInspector } from "@/components/logs/RunInspector";
import { TriggerBadge } from "@/components/logs/WorkflowRunTable";
import type { NodeInfo } from "@/components/flow/run/NodeRunCard";
import { NodeTimeline } from "@/components/flow/run/NodeTimeline";
import { OutputsView } from "@/components/flow/run/OutputsView";
import { PendingPrompt } from "@/components/flow/run/PendingPrompt";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { KeyValueList, Panel } from "@/components/ui/layout";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import { formatCost } from "@/features/settings/usage";
import { flowFileUrl } from "@/features/webapp/flows";
import {
  fetchWorkflowNodeRuns,
  useWfRun,
  useWfRunEventLog,
  useWorkflowVersion,
  workflowKeys,
} from "@/features/workflows/api";
import { normalizeGraph } from "@/features/workflows/graph";
import "@/features/workflows/i18n";
import { shortRunId, wfRunDuration } from "@/features/workflows/runLogFilters";
import { consoleWfRunApi, WfRunController } from "@/features/workflows/wfRunController";
import { isWfRunOpen, stateFromRun } from "@/features/workflows/wfRunStore";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import { nodeLabel } from "@/components/flow/labels";

function useNodeInfo(version: WorkflowVersionOut | undefined) {
  const { t } = useTranslation("workflows");
  return useMemo(() => {
    const nodes = new Map(
      normalizeGraph(version?.graph).nodes.map((node) => [
        node.id,
        {
          type: node.type,
          title: node.title || nodeLabel(t, node.type),
          parentId: node.parent_id,
        } satisfies NodeInfo,
      ]),
    );
    return (nodeId: string) => nodes.get(nodeId);
  }, [version, t]);
}

function RunBody({ workflowId, run }: { workflowId: string; run: WfRunDetail }) {
  const { t } = useTranslation("workflows");
  const { t: tl } = useTranslation("logs");
  const { t: tc } = useTranslation();
  const queryClient = useQueryClient();
  const [controller] = useState(() => new WfRunController(consoleWfRunApi));
  const [replayed, setReplayed] = useState(false);
  const state = useStore(
    controller.store,
    useShallow((s) => s),
  );
  const meta = useStore(
    controller.meta,
    useShallow((m) => m),
  );
  const open = isWfRunOpen(state.status);
  const events = useWfRunEventLog(run.id, { live: open });
  const version = useWorkflowVersion(workflowId, run.wf_version_id);
  const nodeInfo = useNodeInfo(version.data);
  const titleOf = (nodeId: string) => nodeInfo(nodeId)?.title ?? nodeId;

  useEffect(() => {
    controller.configure({
      onError: (error) => toast.error(errorMessage(error, tc)),
      onSettled: () => {
        setReplayed(true);
        void queryClient.invalidateQueries({ queryKey: workflowKeys.run(run.id) });
        void queryClient.invalidateQueries({ queryKey: workflowKeys.eventLog(run.id) });
      },
    });
  });
  // 저장된 이벤트를 처음부터 다시 받아 그리고, 도는 런이면 끝날 때까지 따라간다.
  useEffect(() => {
    controller.attach(run.id);
    return () => controller.dispose();
  }, [controller, run.id]);
  // 런 상세의 노드 런 요약으로 자식 런 id를 채운다. 이벤트가 보존 기간으로 지워졌으면 요약으로 그린다.
  useEffect(() => {
    const actions = controller.store.getState().actions;
    if (replayed && controller.store.getState().status === "idle") {
      actions.load(stateFromRun(run, run.node_runs));
    } else {
      actions.mergeNodeRuns(run.node_runs);
    }
  }, [controller, run, replayed]);

  const renderChild = (item: { childRunId: string | null }) =>
    item.childRunId ? <RunInspector runId={item.childRunId} kind="node_child" nested /> : null;

  const usage = run.usage;
  const versionLabel =
    version.data?.version_number !== null && version.data?.version_number !== undefined
      ? t("runPage.version", { version: version.data.version_number })
      : t("runPage.draft");

  return (
    <div className="flex flex-col gap-4">
      <Panel>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <RunStatusBadge status={run.status} />
            <TriggerBadge trigger={run.trigger} />
            {version.data ? <Badge>{versionLabel}</Badge> : null}
            <span className="font-mono text-xs text-ink-subtle">{run.id}</span>
            {open ? (
              <Button
                size="sm"
                className="ml-auto"
                loading={meta.stopping}
                onClick={() => void controller.stop()}
              >
                <Square className="size-3.5" aria-hidden />
                {t("run.stop")}
              </Button>
            ) : null}
          </div>
          <KeyValueList
            className="grid-cols-[max-content_1fr] text-xs sm:grid-cols-[max-content_1fr_max-content_1fr]"
            items={[
              { label: tl("inspector.queued"), value: formatDateTime(run.queued_at) },
              { label: tl("inspector.ended"), value: formatDateTime(run.ended_at) },
              { label: tl("inspector.duration"), value: formatDuration(wfRunDuration(run)) },
              { label: t("logs.nodes"), value: formatNumber(run.node_run_count) },
              {
                label: tl("inspector.tokens"),
                value: tl("tokensPair", {
                  input: formatNumber(usage.input_tokens),
                  output: formatNumber(usage.output_tokens),
                }),
              },
              { label: tl("inspector.cost"), value: formatCost(usage.cost_usd) },
            ]}
          />
          {run.error ? (
            <Alert tone="danger" title={t("run.error", { code: run.error.code })}>
              {run.error.node_id ? `${titleOf(run.error.node_id)}: ` : ""}
              {run.error.message}
            </Alert>
          ) : null}
          {meta.connection ? (
            <ConnectionNotice status={meta.connection.status} attempt={meta.connection.attempt} />
          ) : null}
          {state.pendingInteraction && state.status === "waiting_input" ? (
            <PendingPrompt
              pending={state.pendingInteraction}
              nodeTitle={titleOf(state.pendingInteraction.nodeId)}
              busy={meta.responding}
              onRespond={(input) => void controller.respond(input)}
            />
          ) : null}
        </div>
      </Panel>
      <Tabs defaultValue="nodes">
        <TabsList aria-label={t("runPage.tabs")}>
          <TabsTrigger value="nodes">{t("runPage.nodesTab")}</TabsTrigger>
          <TabsTrigger value="io">{t("runPage.ioTab")}</TabsTrigger>
          <TabsTrigger value="events">
            {tl("inspector.eventsTab", { count: events.data?.items.length ?? 0 })}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="nodes" className="pt-3">
          <NodeTimeline
            state={state}
            nodeInfo={nodeInfo}
            renderChild={renderChild}
            loadIteration={async (iterationNodeId) => {
              const items = await fetchWorkflowNodeRuns(run.id, iterationNodeId);
              controller.store.getState().actions.mergeNodeRuns(items);
              return items;
            }}
          />
        </TabsContent>
        <TabsContent value="io" className="flex flex-col gap-4 pt-3">
          <Panel title={t("runPage.inputs")}>
            <div className="max-h-96 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs text-code-ink">
              <JsonTree value={run.inputs} />
            </div>
          </Panel>
          <Panel title={t("run.outputs")}>
            {run.outputs ? (
              <OutputsView
                outputs={run.outputs}
                // 콘솔에는 wf 파일 내려받기 경로가 아직 없다. 웹앱 런은 웹앱 경로로 받는다.
                fileUrl={
                  run.trigger === "webapp" ? (fileId) => flowFileUrl(run.id, fileId) : undefined
                }
              />
            ) : (
              <p className="text-sm text-ink-muted">{t("runPage.noOutputs")}</p>
            )}
          </Panel>
        </TabsContent>
        <TabsContent value="events" className="pt-3">
          {events.isPending ? (
            <Spinner label={tl("inspector.loading")} />
          ) : events.isError ? (
            <ErrorAlert
              error={events.error}
              title={tl("inspector.loadFailed")}
              onRetry={() => void events.refetch()}
            />
          ) : (
            <EventTable events={events.data.items} truncated={events.data.truncated} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

// 워크플로우 런 상세(설계 7.2 /workflows/:id/logs/:rid): 노드 타임라인·노드 런 상세·자식 런·이벤트 표.
export default function WorkflowRunPage() {
  const { t } = useTranslation("workflows");
  const { id = "", rid = "" } = useParams();
  const run = useWfRun(rid);
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-6">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="sm" variant="ghost">
          <Link to={`/workflows/${encodeURIComponent(id)}/logs`}>
            <ArrowLeft className="size-4" aria-hidden />
            {t("runPage.back")}
          </Link>
        </Button>
        <h1 className="min-w-0 truncate text-lg font-semibold text-ink">
          {t("runPage.title", { id: shortRunId(rid) })}
        </h1>
        <Button asChild size="sm" variant="ghost" className="ml-auto">
          <Link to={`/workflows/${encodeURIComponent(id)}`}>
            <Workflow className="size-3.5" aria-hidden />
            {t("logs.openEditor")}
          </Link>
        </Button>
      </div>
      {run.isPending ? (
        <PageSpinner label={t("runPage.loading")} />
      ) : run.isError ? (
        <ErrorAlert
          error={run.error}
          title={t("runPage.loadFailed")}
          onRetry={() => void run.refetch()}
        />
      ) : (
        <RunBody key={run.data.id} workflowId={id} run={run.data} />
      )}
    </div>
  );
}
