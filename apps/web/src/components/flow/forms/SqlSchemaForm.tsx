import { X } from "lucide-react";
import { useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Checkbox, Switch } from "@/components/ui/toggle";
import { useSqlSource } from "@/features/sql/api";
import "@/features/sql/i18n";
import {
  sqlSchemaFromForm,
  sqlSchemaSchema,
  sqlSchemaToForm,
  type SqlSchemaForm as FormValues,
} from "@/features/workflows/configSchemas";
import { toNumber } from "@/lib/formSchemas";
import {
  errorAt,
  FieldMessage,
  Section,
  useNodeForm,
  useNodeIssues,
  useServerIssue,
  type NodeFormProps,
} from "./shared";
import { SqlSourceField } from "./SqlSourceField";

// 모델에 줄 수 있는 표: 숨기지 않았고 준비 때 오류가 없는 표·뷰(서버 schema_text가 고르는 것과 같다).
function visibleTables(source: SqlSourceOut | undefined): string[] {
  if (!source?.schema) return [];
  const notes = source.annotations.tables ?? {};
  return source.schema.tables
    .filter((table) => !table.error && !notes[table.name]?.hidden)
    .map((table) => table.name);
}

/*
 * sql_schema 노드 폼(3단계 설계 10.3, 12.3): 데이터 소스, 넣을 표(비우면 전부), 샘플값, 토큰 상한. 결과 DDL은
 * SQL을 만드는 LLM의 시스템 프롬프트에 넣는다. 스키마 글이 모델로 나간다는 데이터 정책 문구를 붙인다.
 */
export function SqlSchemaForm({ node }: NodeFormProps) {
  const { t } = useTranslation("sql");
  const { t: tw } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => sqlSchemaSchema(tc, tw), [tc, tw]);
  const form = useNodeForm<FormValues>(node, schema, sqlSchemaToForm, sqlSchemaFromForm);
  const serverIssue = useServerIssue(node.id);
  const issues = useNodeIssues(node.id);
  const sourceId = useWatch({ control: form.control, name: "source_id" });
  const tables = useWatch({ control: form.control, name: "tables" });
  const detail = useSqlSource(sourceId);
  // 준비 전(schema 없음)에는 고른 표가 있는지 알 수 없어 "없는 표"로 띄우지 않는다.
  const unprepared = detail.data !== undefined && !detail.data.schema;
  const visible = visibleTables(detail.data);
  const missing = detail.data?.schema ? tables.filter((name) => !visible.includes(name)) : [];

  function setTables(next: string[]) {
    form.setValue("tables", next, { shouldDirty: true, shouldValidate: true });
  }

  return (
    <div className="flex flex-col gap-4">
      <Controller
        control={form.control}
        name="source_id"
        render={({ field }) => (
          <SqlSourceField
            value={field.value}
            onChange={(id) => {
              field.onChange(id);
              // 다른 소스의 표 이름은 맞지 않는다.
              if (id !== sourceId) setTables([]);
            }}
            error={form.formState.errors.source_id?.message}
            issues={issues}
          />
        )}
      />
      <Section title={t("node.schema.tables")} hint={t("node.schema.tablesHint")}>
        {!sourceId ? null : detail.isPending ? (
          <Spinner className="size-4" label={tc("state.loading")} />
        ) : unprepared ? (
          <p className="text-xs text-ink-muted">{t("node.schema.notPrepared")}</p>
        ) : visible.length === 0 && missing.length === 0 ? (
          <p className="text-xs text-ink-muted">{t("node.schema.noTables")}</p>
        ) : (
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
            {visible.map((name) => {
              const id = `sql-schema-${node.id}-${name}`;
              return (
                <li key={name} className="flex items-center gap-2.5 px-2.5 py-1.5">
                  <Checkbox
                    id={id}
                    checked={tables.includes(name)}
                    onCheckedChange={(checked) =>
                      setTables(
                        checked === true
                          ? [...tables, name]
                          : tables.filter((item) => item !== name),
                      )
                    }
                  />
                  <label htmlFor={id} className="font-mono text-xs text-ink">
                    {name}
                  </label>
                </li>
              );
            })}
            {missing.map((name) => (
              <li key={name} className="flex items-center gap-2.5 px-2.5 py-1.5">
                <span className="flex-1 font-mono text-xs text-danger">
                  {t("node.schema.missingTable", { name })}
                </span>
                <Button
                  size="iconSm"
                  variant="ghost"
                  aria-label={tc("actions.delete")}
                  title={tc("actions.delete")}
                  onClick={() => setTables(tables.filter((item) => item !== name))}
                >
                  <X className="size-3.5" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <FieldMessage message={errorAt(form, "tables", serverIssue)} />
      </Section>
      <Field inline label={t("node.schema.samples")} hint={t("node.schema.samplesHint")}>
        <Controller
          control={form.control}
          name="include_samples"
          render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />}
        />
      </Field>
      <Field
        label={t("node.schema.maxTokens")}
        hint={t("node.schema.maxTokensHint")}
        error={errorAt(form, "max_tokens", serverIssue)}
      >
        <Input
          type="number"
          step="1"
          className="w-32"
          {...form.register("max_tokens", { setValueAs: toNumber })}
        />
      </Field>
      <p className="text-xs text-ink-muted">
        {t("node.schema.usage", { example: `{{ ${node.id}.ddl }}` })}
      </p>
      <Alert tone="info" title={t("policy.results")} className="text-xs">
        {t("node.schema.policy")}
      </Alert>
    </div>
  );
}
