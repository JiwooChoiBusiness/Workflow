import { useState } from "react";
import { useSearchParams } from "react-router";
import { Panel } from "@/components/ui/layout";
import { useMergedWorkflowRuns, useWorkflows } from "@/features/workflows/api";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  parseWfRunFilters,
  wfRunFiltersToParams,
  type WfRunFilters,
} from "@/features/workflows/runLogFilters";
import { WorkflowRunFilterBar, WorkflowRunTable } from "./WorkflowRunTable";

const PAGE = 50;
// 서버 목록 한 번에 받는 최대 수(GET /workflows/{id}/runs?limit)
const MAX_LIMIT = 200;

// /logs의 워크플로우 런 탭(설계 7.2). 고른 워크플로우가 없으면 모든 워크플로우의 런을 시작 시각 순으로 섞는다.
export function WorkflowRunLog() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = parseWfRunFilters(searchParams);
  const workflows = useWorkflows();
  const [limit, setLimit] = useState(PAGE);
  const list = (workflows.data ?? []).map((item) => ({ id: item.id, name: item.name }));
  const names = new Map(list.map((item) => [item.id, item.name]));
  const ids = filters.workflow ? [filters.workflow] : list.map((item) => item.id);
  const runs = useMergedWorkflowRuns(ids, filters, limit);

  const update = (next: WfRunFilters) => {
    setLimit(PAGE);
    setSearchParams(wfRunFiltersToParams(next, searchParams), { replace: true });
  };
  const noWorkflows = workflows.isSuccess && ids.length === 0;

  return (
    <>
      <div className="mb-4">
        <WorkflowRunFilterBar filters={filters} workflows={list} onChange={update} />
      </div>
      <Panel bodyClassName="p-0">
        <WorkflowRunTable
          runs={noWorkflows ? [] : (runs.data?.items ?? [])}
          loading={workflows.isPending || (!noWorkflows && runs.isPending)}
          error={workflows.error ?? runs.error}
          onRetry={() => {
            void workflows.refetch();
            void runs.refetch();
          }}
          hasMore={Boolean(runs.data?.more) && limit < MAX_LIMIT}
          loadingMore={runs.isFetching && !runs.isPending}
          onMore={() => setLimit((current) => Math.min(current + PAGE, MAX_LIMIT))}
          filtered={hasWfRunFilters(filters)}
          onClearFilters={() => update(DEFAULT_WF_RUN_FILTERS)}
          workflowNames={names}
        />
      </Panel>
    </>
  );
}
