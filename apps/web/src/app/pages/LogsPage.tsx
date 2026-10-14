import { Bot } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router";
import { LogFilterBar } from "@/components/logs/LogFilterBar";
import { RunTable, SessionTable } from "@/components/logs/LogTables";
import { WorkflowRunLog } from "@/components/logs/WorkflowRunLog";
import { Button } from "@/components/ui/button";
import { PageHeader, Panel } from "@/components/ui/layout";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAgents } from "@/features/agents/api";
import { useAgentOrNull } from "@/features/logs/api";
import {
  clearFilters,
  hasActiveFilters,
  logFiltersToParams,
  parseLogFilters,
  type LogFilters,
  type LogTab,
} from "@/features/logs/filters";

// /logs와 /agents/:id/logs가 같이 쓴다. 에이전트별 화면에서는 에이전트 필터가 경로로 고정되고 워크플로우 런
// 탭이 없다.
export default function LogsPage() {
  const { t } = useTranslation("logs");
  const { id: lockedAgent } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const parsed = parseLogFilters(searchParams);
  const filters: LogFilters = lockedAgent
    ? { ...parsed, agent: lockedAgent, tab: parsed.tab === "workflows" ? "sessions" : parsed.tab }
    : parsed;
  const agents = useAgents();
  const agent = useAgentOrNull(lockedAgent ?? null);
  const agentList = (agents.data ?? []).map((item) => ({ id: item.id, name: item.name }));
  const agentNames = new Map(agentList.map((item) => [item.id, item.name]));
  const filtered = hasActiveFilters(filters, { ignoreAgent: Boolean(lockedAgent) });

  const update = (next: LogFilters) => {
    setSearchParams(logFiltersToParams(lockedAgent ? { ...next, agent: null } : next), {
      replace: true,
    });
  };
  const clear = () => update(clearFilters(filters, { keepAgent: Boolean(lockedAgent) }));

  const agentName = agent.data?.name ?? (agent.isPending ? "…" : t("deletedAgent"));
  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <PageHeader
        title={lockedAgent ? t("agentTitle", { name: agentName }) : t("title")}
        description={lockedAgent ? t("agentDescription") : t("description")}
        actions={
          lockedAgent && agent.data ? (
            <Button asChild size="sm">
              <Link to={`/agents/${encodeURIComponent(lockedAgent)}`}>
                <Bot className="size-3.5" aria-hidden />
                {t("openBuilder")}
              </Link>
            </Button>
          ) : null
        }
      />
      <Tabs
        value={filters.tab}
        onValueChange={(value) => update({ ...filters, tab: value as LogTab })}
      >
        <TabsList aria-label={t("tabs.label")} className="mb-4">
          <TabsTrigger value="sessions">{t("tabs.sessions")}</TabsTrigger>
          <TabsTrigger value="runs">{t("tabs.runs")}</TabsTrigger>
          {lockedAgent ? null : (
            <TabsTrigger value="workflows">{t("tabs.workflowRuns")}</TabsTrigger>
          )}
        </TabsList>
        {filters.tab === "workflows" ? null : (
          <div className="mb-4">
            <LogFilterBar
              filters={filters}
              agents={agentList}
              lockAgent={Boolean(lockedAgent)}
              onChange={update}
            />
          </div>
        )}
        <TabsContent value="sessions">
          <Panel bodyClassName="p-0">
            <SessionTable
              filters={filters}
              agentNames={agentNames}
              filtered={filtered}
              onClearFilters={clear}
            />
          </Panel>
        </TabsContent>
        <TabsContent value="runs">
          <Panel bodyClassName="p-0">
            <RunTable
              filters={filters}
              filtered={filtered}
              onClearFilters={clear}
              showAgent={!lockedAgent}
            />
          </Panel>
        </TabsContent>
        {lockedAgent ? null : (
          <TabsContent value="workflows">
            <WorkflowRunLog />
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}
