import { useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import { useAgents, useVersions } from "@/features/agents/api";
import {
  AGENT_LIMIT_FIELDS,
  agentFromForm,
  agentSchema,
  agentToForm,
  type AgentNodeForm,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { toNumberOrNull } from "@/lib/formSchemas";
import { PromptEditor } from "../PromptEditor";
import { VariablePicker } from "../VariablePicker";
import { NameVarRows } from "./NameVarRows";
import { errorAt, Section, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

const PICK = "__pick__";
const PUBLISHED = "__published__";

function VersionSelect({
  agentId,
  value,
  onChange,
}: {
  agentId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation("workflows");
  const versions = useVersions(agentId);
  const options = [
    { value: PUBLISHED, label: t("forms.agent.published") },
    ...[...(versions.data ?? [])]
      .sort((a, b) => b.version_number - a.version_number)
      .map((version) => ({
        value: version.id,
        label: t("forms.agent.versionN", { version: version.version_number }),
        hint: version.change_note ?? undefined,
      })),
  ];
  if (value && !options.some((option) => option.value === value)) {
    options.push({ value, label: value.slice(0, 8), hint: undefined });
  }
  return (
    <Select
      value={value || PUBLISHED}
      onValueChange={(next) => onChange(next === PUBLISHED ? "" : next)}
      options={options}
    />
  );
}

// 설계 2.3 agent 노드와 12장. 결과는 오케스트레이션 ResultSpec, 한도는 1단계 RunLimits 모양이다(서버 AgentConfig).
export function AgentForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const { t: ta } = useTranslation("agents");
  const kind = useFlow((state) => state.kind);
  const schema = useMemo(() => agentSchema(tc, t), [tc, t]);
  const form = useNodeForm<AgentNodeForm>(node, schema, agentToForm, agentFromForm);
  const serverIssue = useServerIssue(node.id);
  const agents = useAgents();
  const agentId = useWatch({ control: form.control, name: "agent_id" });
  const format = useWatch({ control: form.control, name: "result_format" });
  const limitsEnabled = useWatch({ control: form.control, name: "limits_enabled" });
  const agentOptions = [
    { value: PICK, label: t("forms.agent.pick"), disabled: true },
    ...(agents.data ?? []).map((agent) => ({
      value: agent.id,
      label: agent.name,
      hint: agent.published_version_number
        ? t("forms.agent.versionN", { version: agent.published_version_number })
        : undefined,
    })),
  ];

  return (
    <div className="flex flex-col gap-4">
      {agents.isError ? (
        <ErrorAlert error={agents.error} title={t("forms.agent.loadFailed")} />
      ) : null}
      <div className="grid grid-cols-[minmax(0,1fr)_9rem] gap-2">
        <Field
          label={t("forms.agent.agent")}
          error={errorAt(form, "agent_id", serverIssue)}
          required
        >
          <Controller
            control={form.control}
            name="agent_id"
            render={({ field }) => (
              <Select
                value={field.value || PICK}
                onValueChange={(value) => {
                  field.onChange(value);
                  form.setValue("agent_version_id", "", { shouldDirty: true });
                }}
                options={agentOptions}
              />
            )}
          />
        </Field>
        <Field label={t("forms.agent.version")}>
          <Controller
            control={form.control}
            name="agent_version_id"
            render={({ field }) =>
              agentId ? (
                <VersionSelect agentId={agentId} value={field.value} onChange={field.onChange} />
              ) : (
                <Select
                  value={PUBLISHED}
                  onValueChange={field.onChange}
                  options={[{ value: PUBLISHED, label: t("forms.agent.published") }]}
                  disabled
                />
              )
            }
          />
        </Field>
      </div>
      <Field label={t("forms.agent.instruction")} error={serverIssue("instruction")}>
        <Controller
          control={form.control}
          name="instruction"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.agent.instruction")}
              minHeight={120}
            />
          )}
        />
      </Field>
      <NameVarRows
        form={form}
        name="inputs"
        nodeId={node.id}
        serverIssue={serverIssue}
        title={t("forms.agent.inputs")}
        addLabel={t("common.add")}
      />
      <Field label={t("forms.agent.files")} error={errorAt(form, "files_var", serverIssue)}>
        <Controller
          control={form.control}
          name="files_var"
          render={({ field }) => (
            <VariablePicker
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              accept={["file", "array[file]"]}
              clearable
            />
          )}
        />
      </Field>
      <Section title={t("forms.agent.result")}>
        <Controller
          control={form.control}
          name="result_format"
          render={({ field }) => (
            <Select
              aria-label={t("forms.agent.result")}
              value={field.value}
              onValueChange={field.onChange}
              options={(["text", "json"] as const).map((value) => ({
                value,
                label: t(`forms.agent.formats.${value}`),
              }))}
            />
          )}
        />
        {format === "json" ? (
          <Field
            label={t("forms.agent.schema")}
            hint={t("forms.agent.schemaHint")}
            error={errorAt(form, "result_schema_text", serverIssue) ?? serverIssue("result.schema")}
          >
            <Controller
              control={form.control}
              name="result_schema_text"
              render={({ field }) => (
                <CodeEditor
                  value={field.value}
                  onChange={field.onChange}
                  language="json"
                  ariaLabel={t("forms.agent.schema")}
                  minHeight={120}
                  maxHeight={320}
                />
              )}
            />
          </Field>
        ) : null}
        <Field inline label={t("forms.agent.resultFiles")}>
          <Controller
            control={form.control}
            name="result_files"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
      </Section>
      <Section title={t("forms.agent.limits")} hint={t("forms.agent.limitsHint")}>
        <Field inline label={t("forms.agent.limits")}>
          <Controller
            control={form.control}
            name="limits_enabled"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
        {limitsEnabled ? (
          <div className="grid grid-cols-2 gap-3">
            {AGENT_LIMIT_FIELDS.map((name) => (
              <Field
                key={name}
                label={ta(`limits.${name}`)}
                hint={ta(`limits.${name}_hint`)}
                error={errorAt(form, `limits.${name}`, serverIssue)}
              >
                <Input
                  type="number"
                  step={name === "max_cost_usd" ? "any" : "1"}
                  {...form.register(`limits.${name}`, { setValueAs: toNumberOrNull })}
                />
              </Field>
            ))}
          </div>
        ) : null}
      </Section>
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("forms.agent.approval")}>
          <Controller
            control={form.control}
            name="approval_mode"
            render={({ field }) => (
              <Select
                value={field.value}
                onValueChange={field.onChange}
                options={(["inherit", "off"] as const).map((value) => ({
                  value,
                  label: t(`forms.agent.approvals.${value}`),
                }))}
              />
            )}
          />
        </Field>
        <Field label={t("forms.agent.session")}>
          <Controller
            control={form.control}
            name="session_mode"
            render={({ field }) => (
              <Select
                value={field.value}
                onValueChange={field.onChange}
                options={(["ephemeral", "per_conversation"] as const).map((value) => ({
                  value,
                  label: t(`forms.agent.sessions.${value}`),
                  disabled: value === "per_conversation" && kind !== "chatflow",
                }))}
              />
            )}
          />
        </Field>
      </div>
    </div>
  );
}
