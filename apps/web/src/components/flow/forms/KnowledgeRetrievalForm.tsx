import { useCallback, useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { KnowledgeBaseOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import { KbChecklist } from "@/components/knowledge/KbChecklist";
import { RetrievalSettingsForm } from "@/components/knowledge/RetrievalSettingsForm";
import { useKnowledgeBases, useMetadataFieldLists } from "@/features/knowledge/api";
import "@/features/knowledge/i18n";
import { retrievalSummary } from "@/features/knowledge/messages";
import { commonFields, type FilterField } from "@/features/knowledge/metadataFilter";
import { DEFAULT_RETRIEVAL, toRetrievalForm } from "@/features/knowledge/retrievalSettings";
import { useModels } from "@/features/settings/api";
import { useKbOptions } from "@/features/workflows/api";
import {
  DEFAULT_KNOWLEDGE_LIMITS,
  knowledgeRetrievalFromForm,
  knowledgeRetrievalSchema,
  knowledgeRetrievalToForm,
  type KnowledgeLimits,
  type KnowledgeRetrievalForm as FormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { parseSelector } from "@/features/workflows/graph";
import { VariablePicker } from "../VariablePicker";
import { MetadataFilterBuilder } from "./MetadataFilterBuilder";
import {
  errorAt,
  FieldMessage,
  Section,
  useNodeForm,
  useNodeIssues,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

const DEFAULT_MODEL = "__default__";
const NO_OPERATORS: Record<string, readonly string[]> = {};

function isSelector(text: string): boolean {
  return parseSelector(text) !== null;
}

// 고른 지식 베이스 중 하나라도 고품질이면 벡터·하이브리드를 고를 수 있다(경제 KB는 늘 전문 검색이다).
function techniqueOf(kbs: readonly KnowledgeBaseOut[]) {
  return kbs.some((kb) => kb.indexing_technique === "high_quality") ? "high_quality" : "economy";
}

function kbIdsOf(config: Record<string, unknown>): string[] {
  return Array.isArray(config.kb_ids)
    ? config.kb_ids.filter((id): id is string => typeof id === "string")
    : [];
}

// 고른 지식 베이스들의 공통 메타데이터 필드. 지식 베이스 목록과 각 필드 목록을 모두 읽기 전에는 null이다.
function useFilterFields(kbIds: readonly string[], kbs: readonly KnowledgeBaseOut[] | undefined) {
  const existing = kbs ? kbIds.filter((id) => kbs.some((kb) => kb.id === id)) : [];
  const { lists, error, retry } = useMetadataFieldLists(existing);
  const none = kbIds.length === 0;
  const listed = kbs !== undefined;
  const fields = useMemo<FilterField[] | null>(
    () => (none ? [] : listed && lists ? commonFields(lists) : null),
    [none, listed, lists],
  );
  return { fields, error, retry };
}

/*
 * knowledge_retrieval 노드 폼(3단계 설계 8.1, 12.3): 지식 베이스 다중 선택(1~max_kbs), 질의 변수, 검색 설정
 * (KB 설정 이어받기 스위치. KB가 둘 이상이면 정해야 한다), 메타데이터 필터. 상한과 연산자는 kb_options다.
 */
export function KnowledgeRetrievalForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const { t: tk } = useTranslation("knowledge");
  const kind = useFlow((state) => state.kind);
  const options = useKbOptions().data ?? null;
  const kbList = useKnowledgeBases();
  const models = useModels();
  const limits = useMemo<KnowledgeLimits>(
    () => ({
      maxKbs: options?.max_kbs ?? DEFAULT_KNOWLEDGE_LIMITS.maxKbs,
      topKMax: options?.top_k_max ?? DEFAULT_KNOWLEDGE_LIMITS.topKMax,
      waitMaxSec: options?.index_wait_max_sec ?? DEFAULT_KNOWLEDGE_LIMITS.waitMaxSec,
    }),
    [options],
  );
  const operators = options?.operators ?? NO_OPERATORS;
  // 공통 필드는 스키마와 config 변환에도 들어가므로 폼보다 먼저 정한다. 폼에서 고른 지식 베이스는 곧바로
  // config에 들어가 node.config.kb_ids가 폼 값과 같다.
  const filterFields = useFilterFields(kbIdsOf(node.config), kbList.data);
  const fields = filterFields.fields;
  const schema = useMemo(
    () => knowledgeRetrievalSchema(tc, t, limits, { operators, fields }),
    [tc, t, limits, operators, fields],
  );
  const fromForm = useCallback(
    (values: FormValues) => knowledgeRetrievalFromForm(values, fields),
    [fields],
  );
  const form = useNodeForm<FormValues>(node, schema, knowledgeRetrievalToForm, fromForm);
  const serverIssue = useServerIssue(node.id);
  const issues = useNodeIssues(node.id);
  const kbIds = useWatch({ control: form.control, name: "kb_ids" });
  const custom = useWatch({ control: form.control, name: "custom_retrieval" });
  const filter = useWatch({ control: form.control, name: "metadata_filter" });
  const retrieval = useWatch({ control: form.control, name: "retrieval" });

  const byId = new Map((kbList.data ?? []).map((kb) => [kb.id, kb]));
  const chosen = kbIds.flatMap((id) => {
    const kb = byId.get(id);
    return kb ? [kb] : [];
  });
  const many = new Set(kbIds).size > 1;
  const paid = chosen.filter((kb) => kb.data_policy.key_tier === "paid");
  const paidWarning = issues.find((issue) => issue.code === "paid_key_required");

  function startCustom(ids: readonly string[]) {
    const first = ids.map((id) => byId.get(id)).find(Boolean);
    form.setValue(
      "retrieval",
      toRetrievalForm(first?.retrieval_model ?? options?.default_retrieval ?? DEFAULT_RETRIEVAL),
      { shouldDirty: true },
    );
    form.setValue("custom_retrieval", true, { shouldDirty: true, shouldValidate: true });
  }

  function setKbs(next: string[]) {
    form.setValue("kb_ids", next, { shouldDirty: true, shouldValidate: true });
    // 여럿이면 검색 설정이 필수다. 이어받기 중이었으면 첫 지식 베이스의 설정에서 시작한다.
    if (new Set(next).size > 1 && !form.getValues("custom_retrieval")) startCustom(next);
  }

  const retrievalError =
    errorAt(form, "custom_retrieval", serverIssue) ??
    serverIssue("retrieval") ??
    serverIssue("retrieval.top_k");
  const modelOptions = [
    { value: DEFAULT_MODEL, label: tk("filter.modelDefault") },
    ...(models.data?.models ?? []).map((entry) => ({ value: entry.id, label: entry.id })),
  ];

  return (
    <div className="flex flex-col gap-4">
      <Section
        title={t("forms.knowledgeRetrieval.kbs")}
        hint={t("forms.knowledgeRetrieval.kbsHint", { max: limits.maxKbs })}
      >
        <KbChecklist
          idPrefix={`kr-${node.id}`}
          kbs={kbList.data}
          loading={kbList.isPending}
          error={kbList.error}
          onRetry={() => void kbList.refetch()}
          selected={kbIds}
          onChange={setKbs}
          max={limits.maxKbs}
          issueFor={(_, index) => serverIssue(`kb_ids.${index}`)}
        />
        <FieldMessage message={errorAt(form, "kb_ids", serverIssue)} />
        {paid.length > 0 ? (
          <Alert tone="warn" title={t("forms.knowledge.paidTitle")} className="text-xs">
            {options && !options.paid_key_configured
              ? t("forms.knowledge.paidMissing")
              : (paidWarning?.message ?? t("forms.knowledge.paidHint"))}
          </Alert>
        ) : null}
      </Section>
      <Field
        label={t("forms.knowledgeRetrieval.query")}
        hint={
          kind === "chatflow"
            ? t("forms.knowledgeRetrieval.queryHintChat")
            : t("forms.knowledgeRetrieval.queryHintWorkflow")
        }
        error={errorAt(form, "query_var", serverIssue)}
        required
      >
        <Controller
          control={form.control}
          name="query_var"
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
      <Section
        title={t("forms.knowledgeRetrieval.retrieval")}
        hint={t("forms.knowledgeRetrieval.retrievalHint")}
      >
        <Field
          inline
          label={t("forms.knowledgeRetrieval.inherit")}
          hint={
            many
              ? t("forms.knowledgeRetrieval.inheritMany")
              : !custom && chosen[0]
                ? t("forms.knowledgeRetrieval.inheritSummary", {
                    summary: retrievalSummary(chosen[0].retrieval_model, tk),
                  })
                : undefined
          }
        >
          <Switch
            checked={!custom}
            disabled={many && custom}
            onCheckedChange={(inherit) => {
              if (inherit) {
                form.setValue("custom_retrieval", false, {
                  shouldDirty: true,
                  shouldValidate: true,
                });
              } else {
                startCustom(kbIds);
              }
            }}
          />
        </Field>
        {custom ? (
          <RetrievalSettingsForm
            value={retrieval}
            onChange={(next) => form.setValue("retrieval", next, { shouldDirty: true })}
            technique={chosen.length > 0 ? techniqueOf(chosen) : "high_quality"}
            topKMax={limits.topKMax}
            candidatesMax={options?.rerank_candidates_max}
          />
        ) : null}
        <FieldMessage message={retrievalError} />
      </Section>
      <Section title={tk("filter.title")} hint={tk("filter.description")}>
        <MetadataFilterBuilder
          value={filter}
          onChange={(next) =>
            form.setValue("metadata_filter", next, { shouldDirty: true, shouldValidate: true })
          }
          fields={fields}
          fieldsError={kbList.error ?? filterFields.error}
          onRetryFields={() => {
            if (kbList.isError) void kbList.refetch();
            filterFields.retry();
          }}
          operators={operators}
          maxConditions={options?.max_conditions}
          fieldsHint={many ? tk("filter.commonOnly") : undefined}
          isSelector={isSelector}
          issueAt={(path) => serverIssue(`metadata_filter.${path}`)}
          renderVariable={(slot) => (
            <VariablePicker
              nodeId={node.id}
              value={slot.value}
              onChange={slot.onChange}
              aria-label={slot.label}
              aria-invalid={slot.invalid}
            />
          )}
          renderModel={(slot) => (
            <Field label={tk("filter.model")}>
              <Select
                value={slot.value || DEFAULT_MODEL}
                onValueChange={(value) => slot.onChange(value === DEFAULT_MODEL ? "" : value)}
                options={
                  slot.value && !modelOptions.some((option) => option.value === slot.value)
                    ? [...modelOptions, { value: slot.value, label: slot.value }]
                    : modelOptions
                }
              />
            </Field>
          )}
        />
      </Section>
    </div>
  );
}
