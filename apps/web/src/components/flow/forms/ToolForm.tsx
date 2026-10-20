import { useCallback, useMemo } from "react";
import { Controller, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { ToolParam, ToolSpec } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useToolSpecs } from "@/features/workflows/api";
import {
  paramNeedsValue,
  TOOL_ARG_MODES,
  toolFromForm,
  toolParamsForm,
  toolSchema,
  toolToForm,
  type ToolForm as ToolFormValues,
} from "@/features/workflows/configSchemas";
import { PromptEditor } from "../PromptEditor";
import { VariablePicker } from "../VariablePicker";
import {
  errorAt,
  FieldMessage,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

const PICK = "__pick__";

function defaultText(param: ToolParam): string {
  if (param.default === null || param.default === undefined || param.default === "") return "";
  return typeof param.default === "string" ? param.default : JSON.stringify(param.default);
}

// 고정값 칸. 고를 값이 정해진 매개변수와 참·거짓은 고르기, 나머지는 글·숫자 칸이다.
function ConstInput({
  form,
  param,
  label,
}: {
  form: UseFormReturn<ToolFormValues>;
  param: ToolParam;
  label: string;
}) {
  const { t } = useTranslation("workflows");
  const name = `params.${param.name}.value_text` as const;
  const choices = param.options ?? (param.type === "boolean" ? ["true", "false"] : null);
  if (choices) {
    return (
      <Controller
        control={form.control}
        name={name}
        render={({ field }) => (
          <Select
            aria-label={label}
            value={field.value || PICK}
            onValueChange={field.onChange}
            options={[
              { value: PICK, label: t("forms.tool.pickValue"), disabled: true },
              ...choices.map((choice) => ({ value: choice, label: choice })),
            ]}
          />
        )}
      />
    );
  }
  return (
    <Input
      aria-label={label}
      className="font-mono"
      inputMode={param.type === "number" ? "decimal" : undefined}
      placeholder={defaultText(param)}
      {...form.register(name)}
    />
  );
}

function ParamRow({
  form,
  param,
  nodeId,
  serverIssue,
}: {
  form: UseFormReturn<ToolFormValues>;
  param: ToolParam;
  nodeId: string;
  serverIssue: (path: string) => string | undefined;
}) {
  const { t } = useTranslation("workflows");
  const path = `params.${param.name}` as const;
  const mode = useWatch({ control: form.control, name: `${path}.mode` }) ?? "template";
  const label = param.label || param.name;
  const fallback = defaultText(param);
  const hint = [param.description, fallback ? t("forms.tool.default", { value: fallback }) : ""]
    .filter(Boolean)
    .join(" · ");
  const error =
    errorAt(form, `${path}.value_text`, serverIssue) ??
    serverIssue(`${path}.value`) ??
    serverIssue(path);
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-line p-2.5">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-ink">
            {label}
            {paramNeedsValue(param) ? <span className="ml-0.5 text-danger">*</span> : null}
            <span className="ml-1.5 font-mono text-xs font-normal text-ink-subtle">
              {param.name}: {param.type}
            </span>
          </p>
          {hint ? <p className="mt-0.5 text-xs text-ink-muted">{hint}</p> : null}
        </div>
        <Controller
          control={form.control}
          name={`${path}.mode`}
          render={({ field }) => (
            <Select
              aria-label={t("forms.tool.modeFor", { name: label })}
              className="h-7 w-24 shrink-0 text-xs"
              value={field.value}
              onValueChange={(next) => {
                field.onChange(next);
                // 모드마다 값의 뜻이 달라 바꾸면 비운다(템플릿 글이 변수 선택자로 읽히지 않게).
                form.setValue(`${path}.value_text`, "", { shouldValidate: true });
              }}
              options={TOOL_ARG_MODES.map((item) => ({
                value: item,
                label: t(`forms.tool.modes.${item}`),
              }))}
            />
          )}
        />
      </div>
      {mode === "var" ? (
        <Controller
          control={form.control}
          name={`${path}.value_text`}
          render={({ field }) => (
            <VariablePicker
              nodeId={nodeId}
              value={field.value}
              onChange={field.onChange}
              aria-label={label}
              aria-invalid={error ? true : undefined}
            />
          )}
        />
      ) : mode === "const" ? (
        <ConstInput form={form} param={param} label={label} />
      ) : (
        <Controller
          control={form.control}
          name={`${path}.value_text`}
          render={({ field }) => (
            <PromptEditor
              nodeId={nodeId}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={label}
              placeholder={fallback}
              singleLine={param.max_length !== null && param.max_length !== undefined}
              minHeight={param.max_length ? undefined : 60}
              invalid={Boolean(error)}
            />
          )}
        />
      )}
      <FieldMessage message={error} />
    </div>
  );
}

function ToolFormBody({ node, tools }: NodeFormProps & { tools: readonly ToolSpec[] }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => toolSchema(tc, t, tools), [tc, t, tools]);
  // 고른 도구에 없는 매개변수를 버리려면 변환에 도구 목록이 필요하다.
  const fromForm = useCallback((values: ToolFormValues) => toolFromForm(values, tools), [tools]);
  const form = useNodeForm<ToolFormValues>(node, schema, toolToForm, fromForm);
  const serverIssue = useServerIssue(node.id);
  const name = useWatch({ control: form.control, name: "tool" });
  const tool = tools.find((item) => item.name === name);
  const options = [
    { value: PICK, label: t("forms.tool.pick"), disabled: true },
    ...tools.map((item) => ({ value: item.name, label: item.label, hint: item.name })),
  ];
  return (
    <div className="flex flex-col gap-4">
      <Field
        label={t("forms.tool.tool")}
        hint={tool?.description}
        error={errorAt(form, "tool", serverIssue)}
      >
        <Controller
          control={form.control}
          name="tool"
          render={({ field }) => (
            <Select
              value={field.value || PICK}
              onValueChange={(next) => {
                form.setValue("params", toolParamsForm(tools.find((item) => item.name === next)), {
                  shouldValidate: true,
                });
                field.onChange(next);
              }}
              options={
                field.value && !tools.some((item) => item.name === field.value)
                  ? [...options, { value: field.value, label: field.value }]
                  : options
              }
            />
          )}
        />
      </Field>
      {tool ? (
        <Section title={t("forms.tool.params")} hint={t("forms.tool.paramsHint")}>
          {tool.params.length === 0 ? (
            <p className="text-xs text-ink-muted">{t("forms.tool.noParams")}</p>
          ) : (
            tool.params.map((param) => (
              <ParamRow
                key={`${tool.name}:${param.name}`}
                form={form}
                param={param}
                nodeId={node.id}
                serverIssue={serverIssue}
              />
            ))
          )}
        </Section>
      ) : null}
    </div>
  );
}

/*
 * tool(설계 2.4): 내장 도구(GET /node-types의 tools)를 고르고 매개변수마다 값을 템플릿·변수·고정값으로 준다.
 * 비운 매개변수는 서버가 도구의 기본값을 쓴다. 출력은 고른 도구가 정한다.
 */
export function ToolForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const tools = useToolSpecs();
  if (tools.isPending) return <Spinner label={t("forms.tool.loading")} />;
  if (tools.isError) {
    return (
      <ErrorAlert
        error={tools.error}
        title={t("forms.tool.loadFailed")}
        onRetry={() => void tools.refetch()}
      />
    );
  }
  return <ToolFormBody node={node} tools={tools.data} />;
}
