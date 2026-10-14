import { Play, ScrollText, Square } from "lucide-react";
import { useCallback, useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { useShallow } from "zustand/react/shallow";
import { isApiError } from "@/api/http";
import type { WorkflowOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { ConnectionNotice } from "@/components/chat/MessageList";
import { RunStatusView } from "@/components/chat/RunStatusBar";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { useRun } from "@/features/logs/api";
import { uploadWorkflowFile, useWfRun } from "@/features/workflows/api";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { nodeById } from "@/features/workflows/graph";
import "@/features/workflows/i18n";
import { startFieldsOf, type FieldValue } from "@/features/workflows/runInputs";
import {
  useWfRun as useWfRunState,
  useWfRunController,
  useWfRunMeta,
} from "@/features/workflows/wfRunContext";
import {
  finishedNodeCount,
  isNodeRunFinished,
  isWfRunOpen,
  runningNode,
  type NodeRunView,
} from "@/features/workflows/wfRunStore";
import { nodeLabel } from "../labels";
import { ChildRunInspector } from "./ChildRunInspector";
import type { NodeInfo } from "./NodeRunCard";
import { NodeTimeline } from "./NodeTimeline";
import { OutputsView } from "./OutputsView";
import { PendingPrompt } from "./PendingPrompt";
import { RunInputsForm } from "./RunInputsForm";

export type CurrentRun = { kind: "draft" } | { kind: "single"; nodeId: string };

// 자식 런의 세션을 모르면(저장된 이벤트에는 자식을 만든 직후의 진행 이벤트가 없다) 자식 런에서 찾는다.
export function ChildByRun({
  childRunId,
  render,
}: {
  childRunId: string;
  render: (sessionId: string) => ReactNode;
}) {
  const run = useRun(childRunId);
  return run.data ? <>{render(run.data.session_id)}</> : null;
}

/*
 * 편집기 오른쪽의 실행 탭(설계 7.3 RunPanel): start 필드로 만든 입력 폼, 실행·중지, 상태 한 줄, 결과, 노드
 * 타임라인. 실행 시작(저장 먼저)은 편집기가 하고, 런은 편집기가 가진 컨트롤러에 있어 탭을 옮겨도 이어진다.
 */
export function RunPanel({
  workflow,
  values,
  onValuesChange,
  fieldErrors,
  startError,
  onRun,
  current,
  focusId,
}: {
  workflow: WorkflowOut;
  values: Record<string, FieldValue>;
  onValuesChange: (values: Record<string, FieldValue>) => void;
  fieldErrors: Record<string, string>;
  startError: unknown;
  onRun: () => void;
  current: CurrentRun;
  focusId: string | null;
}) {
  const { t } = useTranslation("workflows");
  const controller = useWfRunController();
  const graph = useFlow((state) => state.graph);
  const fields = useMemo(() => startFieldsOf(graph), [graph]);
  const state = useWfRunState(useShallow((s) => s));
  const meta = useWfRunMeta(useShallow((m) => m));
  const open = isWfRunOpen(state.status);
  const finished = state.runId !== null && !open && state.status !== "idle";
  const detail = useWfRun(finished && state.outputsTruncated ? state.runId : null);
  const outputs = detail.data?.outputs ?? state.outputs;
  const chatflow = workflow.kind === "chatflow";

  const nodeInfo = useCallback(
    (nodeId: string): NodeInfo | undefined => {
      const node = nodeById(graph, nodeId);
      return node
        ? {
            type: node.type,
            title: node.title || nodeLabel(t, node.type),
            parentId: node.parent_id,
          }
        : undefined;
    },
    [graph, t],
  );
  const titleOf = (nodeId: string) => nodeInfo(nodeId)?.title ?? nodeId;

  const pendingRun = state.pendingInteraction
    ? (Object.values(state.nodeRuns).find(
        (item) =>
          item.nodeId === state.pendingInteraction?.nodeId && item.status === "waiting_input",
      ) ?? null)
    : null;
  // 자식 세션을 열 수 있으면 노드 카드 안의 인스펙터가 승인 카드를 띄운다.
  const pendingInCard = Boolean(pendingRun?.childSessionId ?? pendingRun?.childRunId);

  const renderChild = (item: NodeRunView) => {
    const live = !isNodeRunFinished(item.status);
    const label = item.title ?? titleOf(item.nodeId);
    const inspector = (sessionId: string) => (
      <ChildRunInspector
        sessionId={sessionId}
        live={live}
        agentLabel={label}
        onResponded={() => controller.resume()}
      />
    );
    if (item.childSessionId) return inspector(item.childSessionId);
    return item.childRunId ? <ChildByRun childRunId={item.childRunId} render={inspector} /> : null;
  };

  return (
    <div className="flex flex-col gap-4 p-4">
      {chatflow ? <Alert tone="info" title={t("run.chatflowNotice")} /> : null}
      <section aria-label={t("run.inputs")} className="flex flex-col gap-3">
        <h3 className="text-sm font-semibold text-ink">{t("run.inputs")}</h3>
        <RunInputsForm
          fields={fields}
          values={values}
          onChange={(name, value) => onValuesChange({ ...values, [name]: value })}
          errors={fieldErrors}
          upload={(file) => uploadWorkflowFile(workflow.id, file)}
          disabled={open || meta.starting}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            loading={meta.starting}
            disabled={open || chatflow}
            onClick={onRun}
          >
            <Play className="size-3.5" aria-hidden />
            {t("run.start")}
          </Button>
          {open ? (
            <Button loading={meta.stopping} onClick={() => void controller.stop()}>
              <Square className="size-3.5" aria-hidden />
              {t("run.stop")}
            </Button>
          ) : null}
          {state.runId ? (
            <Button asChild variant="ghost" size="sm" className="ml-auto">
              <Link to={`/workflows/${workflow.id}/logs/${state.runId}`}>
                <ScrollText className="size-3.5" aria-hidden />
                {t("run.openLog")}
              </Link>
            </Button>
          ) : null}
        </div>
      </section>
      {startError ? (
        <ErrorAlert
          error={startError}
          title={
            isApiError(startError, 422) && startError.code === "draft_invalid"
              ? t("run.draftInvalid")
              : t("run.startFailed")
          }
        />
      ) : null}
      {state.runId === null ? (
        startError ? null : (
          <EmptyState title={t("run.empty")} hint={t("run.emptyHint")} />
        )
      ) : (
        <section aria-label={t("run.result")} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            {current.kind === "single" ? (
              <p className="text-xs font-medium text-ink-muted">
                {t("run.singleTitle", { name: titleOf(current.nodeId) })}
              </p>
            ) : null}
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
            pendingInCard ? (
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
              />
            )
          ) : null}
          {state.status === "succeeded" ? (
            <div className="rounded-md border border-line p-3">
              <h3 className="mb-2 text-sm font-semibold text-ink">{t("run.outputs")}</h3>
              <OutputsView outputs={outputs} truncated={state.outputsTruncated && !detail.data} />
            </div>
          ) : null}
          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold text-ink">{t("run.timeline")}</h3>
            <NodeTimeline
              state={state}
              nodeInfo={nodeInfo}
              focusId={focusId}
              renderChild={renderChild}
            />
          </div>
        </section>
      )}
    </div>
  );
}
