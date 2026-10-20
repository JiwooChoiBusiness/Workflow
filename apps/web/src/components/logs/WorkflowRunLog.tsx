import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";
import { Panel } from "@/components/ui/layout";
import { useWorkflows, useWorkspaceWfRuns } from "@/features/workflows/api";
import "@/features/workflows/i18n";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  parseWfRunFilters,
  wfRunFiltersToParams,
  type WfRunFilters,
} from "@/features/workflows/runLogFilters";
import { WorkflowRunFilterBar, WorkflowRunTable } from "./WorkflowRunTable";

// /logs의 워크플로우 런 탭(설계 7.2). 워크스페이스 전체 런을 최근 것부터 받고 지운 워크플로우의 런도 보인다.
export function WorkflowRunLog() {
  const { t } = useTranslation("workflows");
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = parseWfRunFilters(searchParams);
  const workflows = useWorkflows();
  const runs = useWorkspaceWfRuns(filters);
  const list = (workflows.data ?? []).map((item) => ({ id: item.id, name: item.name }));
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  const names = new Map(
    items.map((run) => [
      run.workflow_id,
      run.workflow_deleted ? t("logs.deletedName", { name: run.workflow_name }) : run.workflow_name,
    ]),
  );

  const update = (next: WfRunFilters) => {
    setSearchParams(wfRunFiltersToParams(next, searchParams), { replace: true });
  };

  return (
    <>
      <div className="mb-4">
        <WorkflowRunFilterBar filters={filters} workflows={list} onChange={update} />
      </div>
      <Panel bodyClassName="p-0">
        <WorkflowRunTable
          runs={items}
          loading={runs.isPending}
          error={runs.error}
          onRetry={() => void runs.refetch()}
          hasMore={runs.hasNextPage}
          loadingMore={runs.isFetchingNextPage}
          onMore={() => void runs.fetchNextPage()}
          filtered={hasWfRunFilters(filters)}
          onClearFilters={() => update(DEFAULT_WF_RUN_FILTERS)}
          workflowNames={names}
        />
      </Panel>
    </>
  );
}
