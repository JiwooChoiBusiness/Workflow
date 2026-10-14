import { Workflow } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router";
import { isApiError } from "@/api/http";
import { WorkflowRunFilterBar, WorkflowRunTable } from "@/components/logs/WorkflowRunTable";
import { Button } from "@/components/ui/button";
import { PageHeader, Panel } from "@/components/ui/layout";
import { useWorkflow, useWorkflowRunPages } from "@/features/workflows/api";
import "@/features/workflows/i18n";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  parseWfRunFilters,
  wfRunFiltersToParams,
  type WfRunFilters,
} from "@/features/workflows/runLogFilters";

// 워크플로우 하나의 실행 기록(설계 7.2 /workflows/:id/logs). 단일 노드 실행은 실행 방식으로 고를 때만 보인다.
export default function WorkflowLogsPage() {
  const { t } = useTranslation("workflows");
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters: WfRunFilters = { ...parseWfRunFilters(searchParams), workflow: null };
  const workflow = useWorkflow(id);
  const runs = useWorkflowRunPages(id, filters);
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered = hasWfRunFilters(filters, { ignoreWorkflow: true });
  const deleted = isApiError(workflow.error, 404);
  const name = workflow.data?.name ?? (deleted ? t("logs.deletedWorkflow") : "…");

  const update = (next: WfRunFilters) =>
    setSearchParams(wfRunFiltersToParams({ ...next, workflow: null }), { replace: true });

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <PageHeader
        title={t("logs.title", { name })}
        description={t("logs.description")}
        actions={
          workflow.data ? (
            <Button asChild size="sm">
              <Link to={`/workflows/${encodeURIComponent(id)}`}>
                <Workflow className="size-3.5" aria-hidden />
                {t("logs.openEditor")}
              </Link>
            </Button>
          ) : null
        }
      />
      <div className="mb-4">
        <WorkflowRunFilterBar filters={filters} onChange={update} />
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
          filtered={filtered}
          onClearFilters={() => update(DEFAULT_WF_RUN_FILTERS)}
        />
      </Panel>
    </div>
  );
}
