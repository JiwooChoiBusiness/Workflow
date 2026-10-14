import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeTypeInfo } from "@/api/types";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { literalText, literalValue } from "@/features/workflows/configSchemas";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import { nodeOutputs, type ErrorStrategy, type FlowNode } from "@/features/workflows/graph";
import { Section } from "./forms/shared";

const STRATEGIES: readonly ErrorStrategy[] = ["fail", "default", "branch"];

// 0~5, 0~10000처럼 범위가 정해진 정수 칸. 범위를 벗어난 값은 그래프에 넣지 않는다(저장 요청 자체가 422가 된다).
function BoundedInteger({
  label,
  hint,
  value,
  min,
  max,
  onCommit,
  error: serverError,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  onCommit: (value: number) => void;
  error?: string;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState(String(value));
  const parsed = Number(text);
  const valid = text.trim() !== "" && Number.isInteger(parsed) && parsed >= min && parsed <= max;
  let error = serverError;
  if (!valid) {
    error = Number.isInteger(parsed)
      ? parsed < min
        ? t("validation.min", { min })
        : t("validation.max", { max })
      : t("validation.integer");
  }
  return (
    <Field label={label} hint={hint} error={error}>
      <Input
        type="number"
        step="1"
        min={min}
        max={max}
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const number = Number(next);
          if (next.trim() !== "" && Number.isInteger(number) && number >= min && number <= max) {
            onCommit(number);
          }
        }}
      />
    </Field>
  );
}

function DefaultOutput({ node, name, type }: { node: FlowNode; name: string; type: string }) {
  const actions = useFlowActions();
  const [text, setText] = useState(() => literalText(node.error.default_outputs[name]));
  function commit(next: string) {
    const outputs = { ...node.error.default_outputs };
    if (next.trim() === "") delete outputs[name];
    else outputs[name] = literalValue(next, type === "string" ? "string" : null);
    actions.updateNode(
      node.id,
      { error: { ...node.error, default_outputs: outputs } },
      `defaults:${node.id}`,
    );
  }
  return (
    <div className="flex items-center gap-2">
      <span
        className="w-28 shrink-0 truncate font-mono text-xs text-ink"
        title={`${name}: ${type}`}
      >
        {name}
      </span>
      <Input
        className="min-w-0 flex-1 font-mono"
        aria-label={name}
        placeholder={type}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          commit(event.target.value);
        }}
      />
    </div>
  );
}

// 노드 공통의 오류 처리(error.strategy, default_outputs)와 재시도(설계 2.2).
export function ErrorRetrySection({
  node,
  info,
}: {
  node: FlowNode;
  info: NodeTypeInfo | undefined;
}) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const issues = useFlow((state) => state.issues);
  const allowed = info?.error_strategies ?? ["fail"];
  if (allowed.length <= 1) {
    return (
      <Section title={t("errorRetry.title")}>
        <p className="text-xs text-ink-muted">{t("errorRetry.controlOnly")}</p>
      </Section>
    );
  }
  const issueFor = (field: string) =>
    issues.find((issue) => issue.node_id === node.id && issue.field?.startsWith(field))?.message;
  const strategy = node.error.strategy;
  const outputs = nodeOutputs(node, info);
  return (
    <Section title={t("errorRetry.title")}>
      <Field
        label={t("errorRetry.strategy")}
        hint={t(`errorRetry.strategyHints.${strategy}`)}
        error={issueFor("error.strategy")}
      >
        <Select
          value={strategy}
          onValueChange={(value) =>
            actions.updateNode(node.id, {
              error: { ...node.error, strategy: value as ErrorStrategy },
            })
          }
          options={STRATEGIES.filter((item) => allowed.includes(item)).map((item) => ({
            value: item,
            label: t(`errorRetry.strategies.${item}`),
          }))}
        />
      </Field>
      {strategy === "default" ? (
        <Field
          label={t("errorRetry.defaults")}
          hint={t("errorRetry.defaultsHint")}
          error={issueFor("error.default_outputs")}
        >
          <div className="flex flex-col gap-1.5">
            {outputs.map((output) => (
              <DefaultOutput key={output.name} node={node} name={output.name} type={output.type} />
            ))}
          </div>
        </Field>
      ) : null}
      <div className="grid grid-cols-2 gap-3">
        <BoundedInteger
          label={t("errorRetry.maxAttempts")}
          hint={t("errorRetry.maxAttemptsHint")}
          value={node.retry.max_attempts}
          min={0}
          max={5}
          error={issueFor("retry.max_attempts")}
          onCommit={(value) =>
            actions.updateNode(
              node.id,
              { retry: { ...node.retry, max_attempts: value } },
              `retry:${node.id}`,
            )
          }
        />
        <BoundedInteger
          label={t("errorRetry.interval")}
          hint={t("errorRetry.intervalHint")}
          value={node.retry.interval_ms}
          min={0}
          max={10_000}
          error={issueFor("retry.interval_ms")}
          onCommit={(value) =>
            actions.updateNode(
              node.id,
              { retry: { ...node.retry, interval_ms: value } },
              `retry:${node.id}`,
            )
          }
        />
      </div>
    </Section>
  );
}
