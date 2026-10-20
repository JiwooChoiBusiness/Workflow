import type { TFunction } from "i18next";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useAgents } from "@/features/agents/api";
import { useToolSpecs } from "@/features/workflows/api";
import type { FlowNode } from "@/features/workflows/graph";

// 노드 카드의 핵심 설정 한 줄(설계 7.1). 분기 노드의 가지 줄은 BranchRows가 그린다.

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map((item) => (typeof item === "object" && item !== null ? item : {}))
    : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function firstLine(value: unknown): string {
  return text(value).trim().split(/\r?\n/)[0] ?? "";
}

function Mono({ children }: { children: string }) {
  return <span className="font-mono">{children}</span>;
}

function Line({ children }: { children: ReactNode }) {
  return <p className="truncate">{children}</p>;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function ToolSummary({ name }: { name: string }) {
  const { t } = useTranslation("workflows");
  const tools = useToolSpecs();
  if (!name) return <Line>{t("card.pickTool")}</Line>;
  const label = tools.data?.find((tool) => tool.name === name)?.label;
  return (
    <Line>
      {label ? <>{label} · </> : null}
      <Mono>{name}</Mono>
    </Line>
  );
}

// 켠 단계만 차례대로(거르기 → 정렬 → 자르기 → 꺼내기).
function listSteps(config: Record<string, unknown>, t: TFunction<"workflows">) {
  const steps: string[] = [];
  const filter = record(config.filter);
  if (filter) steps.push(t("card.listFilter", { count: list(filter.conditions).length }));
  const sort = record(config.sort);
  if (sort) steps.push(t(sort.order === "desc" ? "card.listSortDesc" : "card.listSortAsc"));
  const slice = record(config.slice);
  if (slice) {
    steps.push(
      typeof slice.limit === "number"
        ? t("card.listSlice", { offset: Number(slice.offset ?? 0), limit: slice.limit })
        : t("card.listSkip", { offset: Number(slice.offset ?? 0) }),
    );
  }
  if (typeof config.extract_index === "number") {
    steps.push(t("card.listItem", { index: config.extract_index }));
  }
  return steps;
}

function AgentSummary({ agentId }: { agentId: string }) {
  const { t } = useTranslation("workflows");
  const agents = useAgents();
  if (!agentId) return <Line>{t("card.pickAgent")}</Line>;
  const name = agents.data?.find((agent) => agent.id === agentId)?.name;
  return <Line>{name ?? t("card.agentId", { id: agentId.slice(0, 8) })}</Line>;
}

export function NodeSummary({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const config = node.config;
  switch (node.type) {
    case "start": {
      const fields = list(config.fields)
        .map((field) => text(field.name))
        .filter(Boolean);
      return (
        <Line>
          {fields.length ? (
            <>
              {t("card.inputs", { count: fields.length })} · <Mono>{fields.join(", ")}</Mono>
            </>
          ) : (
            t("card.noInputs")
          )}
        </Line>
      );
    }
    case "end": {
      const outputs = list(config.outputs)
        .map((item) => text(item.name))
        .filter(Boolean);
      return (
        <Line>
          {outputs.length ? (
            <>
              {t("card.outputs", { count: outputs.length })} · <Mono>{outputs.join(", ")}</Mono>
            </>
          ) : (
            t("card.noOutputs")
          )}
        </Line>
      );
    }
    case "answer":
      return <Line>{firstLine(config.text) || t("card.empty")}</Line>;
    case "template": {
      const vars = list(config.vars).length;
      return (
        <Line>
          {t("card.vars", { count: vars })}
          {firstLine(config.template) ? <> · {firstLine(config.template)}</> : null}
        </Line>
      );
    }
    case "variable_aggregator":
      return (
        <Line>
          {t("card.aggregate", {
            count: Array.isArray(config.vars) ? config.vars.length : 0,
            type: text(config.output_type) || "any",
          })}
        </Line>
      );
    case "variable_assigner": {
      const targets = list(config.items)
        .map((item) => text(item.target))
        .filter(Boolean);
      return (
        <Line>
          {t("card.assign", { count: targets.length })}
          {targets.length ? (
            <>
              {" "}
              · <Mono>{targets.join(", ")}</Mono>
            </>
          ) : null}
        </Line>
      );
    }
    case "llm":
    case "question_classifier":
    case "parameter_extractor": {
      const model = (config.model ?? {}) as { model_id?: unknown };
      const modelId = text(model.model_id);
      return (
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-mono">{modelId || t("card.defaultModel")}</span>
          {node.type === "llm" && config.json_mode === true ? (
            <Badge tone="info">{t("card.json")}</Badge>
          ) : null}
          {node.type === "parameter_extractor" ? (
            <span className="shrink-0">
              · {t("card.params", { count: list(config.parameters).length })}
            </span>
          ) : null}
        </div>
      );
    }
    case "http_request":
      return (
        <Line>
          <Mono>{`${text(config.method) || "GET"} ${text(config.url) || "…"}`}</Mono>
        </Line>
      );
    case "code":
      return (
        <Line>
          {t("card.code", {
            inputs: list(config.inputs).length,
            outputs: list(config.outputs).length,
          })}
        </Line>
      );
    case "agent":
      return <AgentSummary agentId={text(config.agent_id)} />;
    case "iteration_start":
      return (
        <Line>
          <Mono>{t("card.entry")}</Mono>
        </Line>
      );
    case "loop_start":
      return (
        <Line>
          <Mono>{t("card.loopEntry")}</Mono>
        </Line>
      );
    case "list_operator": {
      const steps = listSteps(config, t);
      return (
        <Line>
          <Mono>{text(config.input_var) || "…"}</Mono>
          {steps.length ? <> · {steps.join(" → ")}</> : null}
        </Line>
      );
    }
    case "document_extractor":
      return (
        <Line>
          {t("card.extractFrom")} <Mono>{text(config.file_var) || "…"}</Mono>
        </Line>
      );
    case "tool":
      return <ToolSummary name={text(config.tool)} />;
    case "human_input": {
      const fields = list(config.fields)
        .map((field) => text(field.name))
        .filter(Boolean);
      return (
        <Line>
          {t("card.form", { count: fields.length })}
          {fields.length ? (
            <>
              {" "}
              · <Mono>{fields.join(", ")}</Mono>
            </>
          ) : null}
        </Line>
      );
    }
    default:
      return null;
  }
}
