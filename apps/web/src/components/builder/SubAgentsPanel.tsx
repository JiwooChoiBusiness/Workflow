import { Settings2 } from "lucide-react";
import { useState } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { AgentOut, AgentWarning } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { Checkbox } from "@/components/ui/toggle";
import { useAgents } from "@/features/agents/api";
import type { AgentForm } from "@/features/agents/schemas";
import { MAX_SUB_AGENTS, newSubAgent, type SubAgentForm } from "@/features/agents/subAgents";
import { SubAgentSettingsDialog } from "./SubAgentSettingsDialog";

// 저장 응답의 경고와 같은 코드(오케스트레이션 2.3)를 에이전트 목록의 값으로 바로 계산해 저장 전에도 보인다.
type ItemWarning = "agent_description_empty" | "agent_unpublished";

function itemWarnings(agent: AgentOut): ItemWarning[] {
  const codes: ItemWarning[] = [];
  if (!agent.description.trim()) codes.push("agent_description_empty");
  if (!agent.published_version_id) codes.push("agent_unpublished");
  return codes;
}

function SettingsSummary({ item }: { item: SubAgentForm }) {
  const { t } = useTranslation("agents");
  const parts = [
    item.format === "text"
      ? t("subAgents.summary.text")
      : item.schema_source === "fixed"
        ? t("subAgents.summary.jsonFixed")
        : t("subAgents.summary.jsonModel"),
    item.files ? null : t("subAgents.summary.noFiles"),
    item.approval_mode === "off" ? t("subAgents.summary.approvalOff") : null,
  ].filter(Boolean);
  return <p className="text-xs text-ink-subtle">{parts.join(" · ")}</p>;
}

function AgentRow({
  agent,
  item,
  index,
  form,
  disabled,
  onToggle,
  onEdit,
}: {
  agent: AgentOut;
  item: SubAgentForm | undefined;
  index: number;
  form: UseFormReturn<AgentForm>;
  disabled: boolean;
  onToggle: (on: boolean) => void;
  onEdit: () => void;
}) {
  const { t } = useTranslation("agents");
  const id = `sub-agent-${agent.id}`;
  const errors = index >= 0 ? form.formState.errors.sub_agents?.[index] : undefined;
  const warnings = item ? itemWarnings(agent) : [];
  return (
    <li className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
      <Checkbox
        id={id}
        className="mt-0.5"
        checked={item !== undefined}
        disabled={disabled}
        aria-label={t("subAgents.include", { name: agent.name })}
        onCheckedChange={(value) => onToggle(value === true)}
      />
      <div className="min-w-0 flex-1">
        <label
          htmlFor={id}
          className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink"
        >
          {agent.name}
          <span className="font-mono text-xs font-normal text-ink-subtle">{agent.slug}</span>
          {agent.published_version_number ? (
            <Badge tone="accent">
              {t("subAgents.published", { version: agent.published_version_number })}
            </Badge>
          ) : (
            <Badge>{t("subAgents.draftOnly")}</Badge>
          )}
          {warnings.map((code) => (
            <Badge key={code} tone="warn" title={t(`subAgents.warning.${code}_hint`)}>
              {t(`subAgents.warning.${code}`)}
            </Badge>
          ))}
        </label>
        {agent.description ? (
          <p className="line-clamp-1 text-xs text-ink-muted">{agent.description}</p>
        ) : null}
        {item ? <SettingsSummary item={item} /> : null}
        {errors?.agent_id?.message ? (
          <p className="mt-0.5 text-xs text-danger" role="alert">
            {errors.agent_id.message}
          </p>
        ) : null}
        {errors?.schema_text?.message ? (
          <p className="mt-0.5 text-xs text-danger" role="alert">
            {t("subAgents.dialog.schema")}: {errors.schema_text.message}
          </p>
        ) : null}
      </div>
      {item ? (
        <Button
          size="sm"
          variant="ghost"
          aria-label={t("subAgents.settingsLabel", { name: agent.name })}
          onClick={onEdit}
        >
          <Settings2 className="size-3.5" aria-hidden />
          {t("subAgents.settings")}
        </Button>
      ) : null}
    </li>
  );
}

// 빌더 '도구와 스킬' 탭의 에이전트 묶음(오케스트레이션 8.2). 체크하면 기본값 항목이 sub_agents에 들어간다.
export function SubAgentsPanel({
  form,
  agentId,
  warnings,
}: {
  form: UseFormReturn<AgentForm>;
  agentId: string;
  // 마지막 저장 응답의 경고
  warnings: readonly AgentWarning[];
}) {
  const { t } = useTranslation("agents");
  const agents = useAgents();
  const items = useWatch({ control: form.control, name: "sub_agents" });
  const [editing, setEditing] = useState<string | null>(null);
  const others = (agents.data ?? []).filter((agent) => agent.id !== agentId);
  const known = new Set(others.map((agent) => agent.id));
  const unknown = agents.isSuccess ? items.filter((item) => !known.has(item.agent_id)) : [];
  const full = items.length >= MAX_SUB_AGENTS;
  const listErrors = form.formState.errors.sub_agents;
  const listError = listErrors?.message ?? listErrors?.root?.message;
  // 항목에 붙지 않는 경고(사슬 깊이 초과 등)
  const general = warnings.filter((warning) => !warning.agent_id);

  const setItems = (next: SubAgentForm[]) =>
    form.setValue("sub_agents", next, { shouldDirty: true, shouldValidate: false });

  function toggle(agent: AgentOut, on: boolean) {
    if (on) {
      if (!items.some((item) => item.agent_id === agent.id)) {
        setItems([...items, newSubAgent(agent.id)]);
      }
    } else {
      setItems(items.filter((item) => item.agent_id !== agent.id));
    }
    form.clearErrors("sub_agents");
  }

  const editingIndex = items.findIndex((item) => item.agent_id === editing);
  const editingAgent = others.find((agent) => agent.id === editing);

  let list;
  if (agents.isPending) {
    list = <Spinner />;
  } else if (agents.isError) {
    list = (
      <ErrorAlert
        error={agents.error}
        title={t("subAgents.loadFailed")}
        onRetry={() => void agents.refetch()}
      />
    );
  } else if (others.length === 0) {
    list = <p className="text-sm text-ink-muted">{t("subAgents.none")}</p>;
  } else {
    list = (
      <ul className="divide-y divide-line">
        {others.map((agent) => {
          const index = items.findIndex((item) => item.agent_id === agent.id);
          const item = index >= 0 ? items[index] : undefined;
          return (
            <AgentRow
              key={agent.id}
              agent={agent}
              item={item}
              index={index}
              form={form}
              disabled={!item && full}
              onToggle={(on) => toggle(agent, on)}
              onEdit={() => setEditing(agent.id)}
            />
          );
        })}
      </ul>
    );
  }

  return (
    <Panel
      title={t("subAgents.title")}
      description={t("subAgents.description")}
      actions={
        <Badge tone={full ? "warn" : "neutral"}>
          {t("subAgents.count", { count: items.length, max: MAX_SUB_AGENTS })}
        </Badge>
      }
    >
      <div className="flex flex-col gap-3">
        {listError ? <Alert tone="danger" title={listError} /> : null}
        {general.map((warning) => (
          <Alert key={warning.code} tone="warn" title={warning.message} />
        ))}
        {full ? (
          <p className="text-xs text-ink-muted">{t("subAgents.limit", { max: MAX_SUB_AGENTS })}</p>
        ) : null}
        {list}
        {unknown.length > 0 ? (
          <div className="flex items-center gap-3 text-sm text-ink-muted">
            {t("subAgents.unknown", { count: unknown.length })}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setItems(items.filter((item) => known.has(item.agent_id)))}
            >
              {t("subAgents.removeUnknown")}
            </Button>
          </div>
        ) : null}
      </div>
      <SubAgentSettingsDialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        agentName={editingAgent?.name ?? ""}
        value={editingIndex >= 0 ? items[editingIndex] : null}
        serverError={
          editingIndex >= 0 ? listErrors?.[editingIndex]?.schema_text?.message : undefined
        }
        onApply={(next) => {
          setItems(items.map((item) => (item.agent_id === next.agent_id ? next : item)));
          if (editingIndex >= 0) form.clearErrors(`sub_agents.${editingIndex}`);
        }}
      />
    </Panel>
  );
}
