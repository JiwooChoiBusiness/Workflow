import { useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { RadioCards } from "@/components/ui/radio-cards";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import { useSqlSource } from "@/features/sql/api";
import "@/features/sql/i18n";
import { useSqlOptions } from "@/features/workflows/api";
import {
  DEFAULT_SQL_LIMITS,
  SQL_APPROVALS,
  SQL_MODES,
  SQL_QUERY_MAX_ROWS,
  sqlQueryFromForm,
  sqlQuerySchema,
  sqlQueryToForm,
  type SqlLimitsOptions,
  type SqlQueryForm as FormValues,
} from "@/features/workflows/configSchemas";
import { toNumberOrNull } from "@/lib/formSchemas";
import { PromptEditor } from "../PromptEditor";
import { VariablePicker } from "../VariablePicker";
import {
  errorAt,
  Section,
  useNodeForm,
  useNodeIssues,
  useServerIssue,
  type NodeFormProps,
} from "./shared";
import { SqlSourceField } from "./SqlSourceField";

/*
 * sql_query 노드 폼(3단계 설계 10.3~10.6, 12.3): 데이터 소스, SQL 템플릿 또는 변수, 행·시간 상한, 실행 전 승인,
 * 모델에 보낼 범위(llm_rows·llm_bytes), 잘린 결과 CSV. 한도와 승인을 비우면 데이터 소스 값을 따르고, 칸 안내에
 * 그 값을 보인다. 결과 표가 모델로 나간다는 데이터 정책 문구를 붙인다.
 */
export function SqlQueryForm({ node }: NodeFormProps) {
  const { t } = useTranslation("sql");
  const { t: tw } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const options = useSqlOptions().data ?? null;
  const limits = useMemo<SqlLimitsOptions>(
    () => ({
      maxRowsMax: options?.max_rows_max ?? DEFAULT_SQL_LIMITS.maxRowsMax,
      timeoutMax: options?.timeout_max ?? DEFAULT_SQL_LIMITS.timeoutMax,
      llmBytesMax: options?.llm_bytes_max ?? DEFAULT_SQL_LIMITS.llmBytesMax,
    }),
    [options],
  );
  const schema = useMemo(() => sqlQuerySchema(tc, tw, limits), [tc, tw, limits]);
  const form = useNodeForm<FormValues>(node, schema, sqlQueryToForm, sqlQueryFromForm);
  const serverIssue = useServerIssue(node.id);
  const issues = useNodeIssues(node.id);
  const sourceId = useWatch({ control: form.control, name: "source_id" });
  const mode = useWatch({ control: form.control, name: "sql_mode" });
  const source = useSqlSource(sourceId).data;

  // 비운 칸의 안내: 데이터 소스를 골랐으면 그 값을 보인다.
  function inherit(value: number | string | undefined): string {
    return value === undefined
      ? t("node.inheritUnknown")
      : t("node.inheritHint", { value: String(value) });
  }
  const sqlIssue = issues.find(
    (issue) => issue.field === "sql" && issue.level === "error",
  )?.message;

  return (
    <div className="flex flex-col gap-4">
      <Controller
        control={form.control}
        name="source_id"
        render={({ field }) => (
          <SqlSourceField
            value={field.value}
            onChange={field.onChange}
            error={form.formState.errors.source_id?.message}
            issues={issues}
          />
        )}
      />
      <Section title={t("node.query.sql")}>
        <Field label={t("node.query.sqlMode")}>
          <Controller
            control={form.control}
            name="sql_mode"
            render={({ field }) => (
              <RadioCards
                ariaLabel={t("node.query.sqlMode")}
                value={field.value}
                onValueChange={(value) => {
                  field.onChange(value);
                  void form.trigger(["sql", "sql_var"]);
                }}
                className="flex-row"
                options={SQL_MODES.map((value) => ({
                  value,
                  label: t(`node.query.sqlModes.${value}`),
                }))}
              />
            )}
          />
        </Field>
        {mode === "template" ? (
          <Field
            label={t("node.query.sql")}
            hint={t("node.query.sqlHint", { example: "{{ llm_1.text }}" })}
            error={errorAt(form, "sql", serverIssue) ?? sqlIssue}
            required
          >
            <Controller
              control={form.control}
              name="sql"
              render={({ field }) => (
                <PromptEditor
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  ariaLabel={t("node.query.sql")}
                  minHeight={96}
                />
              )}
            />
          </Field>
        ) : (
          <Field
            label={t("node.query.sqlVar")}
            hint={t("node.query.sqlVarHint")}
            error={errorAt(form, "sql_var", serverIssue) ?? sqlIssue}
            required
          >
            <Controller
              control={form.control}
              name="sql_var"
              render={({ field }) => (
                <VariablePicker
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  accept={["string"]}
                />
              )}
            />
          </Field>
        )}
      </Section>
      <Section title={t("node.query.limits")}>
        <div className="grid grid-cols-2 gap-3">
          <Field
            label={t("node.query.maxRows")}
            hint={`${t("node.query.maxRowsHint", { limit: SQL_QUERY_MAX_ROWS, max: limits.maxRowsMax })} · ${inherit(source?.limits.max_rows)}`}
            error={errorAt(form, "max_rows", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              placeholder={source ? String(source.limits.max_rows) : undefined}
              {...form.register("max_rows", { setValueAs: toNumberOrNull })}
            />
          </Field>
          <Field
            label={t("node.query.timeout")}
            hint={`${t("node.query.timeoutHint", { max: limits.timeoutMax })} · ${inherit(source?.limits.timeout_sec)}`}
            error={errorAt(form, "timeout_sec", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              placeholder={source ? String(source.limits.timeout_sec) : undefined}
              {...form.register("timeout_sec", { setValueAs: toNumberOrNull })}
            />
          </Field>
        </div>
      </Section>
      <Field
        label={t("node.query.approval")}
        hint={
          source
            ? `${t("node.query.approvalInherit", { value: t(`approval.${source.approval}`) })} · ${t("node.query.approvalHint")}`
            : t("node.query.approvalHint")
        }
      >
        <Controller
          control={form.control}
          name="approval"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={SQL_APPROVALS.map((value) => ({
                value,
                label: t(`node.query.approvals.${value}`),
              }))}
            />
          )}
        />
      </Field>
      <Section title={t("node.query.llm")} hint={t("node.query.llmHint")}>
        <div className="grid grid-cols-2 gap-3">
          <Field
            label={t("node.query.llmRows")}
            hint={inherit(source?.limits.llm_rows)}
            error={errorAt(form, "llm_rows", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              placeholder={source ? String(source.limits.llm_rows) : undefined}
              {...form.register("llm_rows", { setValueAs: toNumberOrNull })}
            />
          </Field>
          <Field
            label={t("node.query.llmBytes")}
            hint={`${t("node.query.llmBytesHint", { max: limits.llmBytesMax })} · ${inherit(source?.limits.llm_bytes)}`}
            error={errorAt(form, "llm_bytes", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              placeholder={source ? String(source.limits.llm_bytes) : undefined}
              {...form.register("llm_bytes", { setValueAs: toNumberOrNull })}
            />
          </Field>
        </div>
      </Section>
      <Field inline label={t("node.query.exportFile")} hint={t("node.query.exportFileHint")}>
        <Controller
          control={form.control}
          name="export_file"
          render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />}
        />
      </Field>
      <Alert tone="info" title={t("policy.results")} className="text-xs">
        {t("node.query.policy")}
      </Alert>
    </div>
  );
}
