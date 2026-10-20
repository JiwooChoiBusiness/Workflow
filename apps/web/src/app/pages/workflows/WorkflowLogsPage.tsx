import { Workflow } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router";
import { isApiError } from "@/api/http";
import type { WfConversationSource } from "@/api/types";
import { CONVERSATION_SOURCES, WfConversationLog } from "@/components/logs/WfConversationLog";
import { WorkflowRunFilterBar, WorkflowRunTable } from "@/components/logs/WorkflowRunTable";
import { Button } from "@/components/ui/button";
import { PageHeader, Panel } from "@/components/ui/layout";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useWorkflow, useWorkflowRunPages } from "@/features/workflows/api";
import "@/features/workflows/i18n";
import {
  DEFAULT_WF_RUN_FILTERS,
  hasWfRunFilters,
  parseWfRunFilters,
  wfRunFiltersToParams,
  type WfRunFilters,
} from "@/features/workflows/runLogFilters";

const LOG_TABS = ["runs", "conversations"] as const;
type LogTab = (typeof LOG_TABS)[number];

function RunsLog({ workflowId }: { workflowId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters: WfRunFilters = { ...parseWfRunFilters(searchParams), workflow: null };
  const runs = useWorkflowRunPages(workflowId, filters);
  const items = runs.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered = hasWfRunFilters(filters, { ignoreWorkflow: true });

  const update = (next: WfRunFilters) =>
    setSearchParams(wfRunFiltersToParams({ ...next, workflow: null }), { replace: true });

  return (
    <>
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
    </>
  );
}

function sourceOf(value: string | null): WfConversationSource | null {
  return CONVERSATION_SOURCES.find((item) => item === value) ?? null;
}

/*
 * 워크플로우 하나의 실행 기록(설계 7.2 /workflows/:id/logs). 단일 노드 실행은 실행 방식으로 고를 때만 보인다.
 * 챗플로우는 대화 탭(?tab=conversations, 고른 대화는 ?cid=)이 더 있고 대화 하나의 런만 런 탭에서 거를 수
 * 있다(?conversation=).
 */
export default function WorkflowLogsPage() {
  const { t } = useTranslation("workflows");
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const workflow = useWorkflow(id);
  const deleted = isApiError(workflow.error, 404);
  const name = workflow.data?.name ?? (deleted ? t("logs.deletedWorkflow") : "…");
  const chatflow = workflow.data?.kind === "chatflow";
  const tab: LogTab =
    chatflow && searchParams.get("tab") === "conversations" ? "conversations" : "runs";

  const header = (
    <PageHeader
      title={t("logs.title", { name })}
      description={chatflow ? t("logs.chatflowDescription") : t("logs.description")}
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
  );

  if (!chatflow) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        {header}
        <RunsLog workflowId={id} />
      </div>
    );
  }

  const conversationParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "conversations");
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    return next;
  };

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      {header}
      <Tabs
        value={tab}
        onValueChange={(value) => {
          const next = new URLSearchParams(searchParams);
          if (value === "conversations") next.set("tab", "conversations");
          else next.delete("tab");
          setSearchParams(next, { replace: true });
        }}
      >
        <TabsList aria-label={t("logs.tabsLabel")} className="mb-4">
          {LOG_TABS.map((key) => (
            <TabsTrigger key={key} value={key}>
              {t(`logs.tabs.${key}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="runs">
          <RunsLog workflowId={id} />
        </TabsContent>
        <TabsContent value="conversations">
          <WfConversationLog
            workflowId={id}
            selected={searchParams.get("cid")}
            source={sourceOf(searchParams.get("source"))}
            onSelect={(cid) => setSearchParams(conversationParams({ cid }), { replace: true })}
            onSourceChange={(source) =>
              setSearchParams(conversationParams({ source, cid: null }), { replace: true })
            }
            onRuns={(cid) =>
              setSearchParams(
                wfRunFiltersToParams({ ...DEFAULT_WF_RUN_FILTERS, conversation: cid }),
              )
            }
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
