import { useMemo } from "react";
import { Controller, useFieldArray, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, type SelectOption } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import { ProcessRuleForm } from "@/components/knowledge/ProcessRuleForm";
import { useKnowledgeBases, useMetadataFieldLists } from "@/features/knowledge/api";
import "@/features/knowledge/i18n";
import { completeRule, ruleErrors } from "@/features/knowledge/processRule";
import { useKbOptions } from "@/features/workflows/api";
import {
  DEFAULT_KNOWLEDGE_LIMITS,
  DUPLICATE_POLICIES,
  knowledgeIndexFromForm,
  knowledgeIndexSchema,
  knowledgeIndexToForm,
  type DuplicatePolicy,
  type KnowledgeIndexForm as FormValues,
  type KnowledgeLimits,
} from "@/features/workflows/configSchemas";
import { toNumber } from "@/lib/formSchemas";
import { PromptEditor } from "../PromptEditor";
import { VariablePicker } from "../VariablePicker";
import {
  AddButton,
  EmptyRows,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useNodeIssues,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

const PICK = "__pick__";
// sys.now는 번역 문자열에 그대로 쓰면 보간으로 읽히므로 값으로 넘긴다.
const NOW_TEMPLATE = "{{ sys.now }}";

/*
 * knowledge_index 노드 폼(3단계 설계 8.2, 12.3): 지식 베이스, 넣을 글 변수나 파일 변수(하나만), 문서 이름
 * 템플릿, 이 문서만 쓸 청크 규칙(M25의 규칙 폼), 메타데이터 값, 같은 이름 문서 처리, 색인 기다리기.
 */
export function KnowledgeIndexForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const options = useKbOptions().data ?? null;
  const kbList = useKnowledgeBases();
  const limits = useMemo<KnowledgeLimits>(
    () => ({
      maxKbs: options?.max_kbs ?? DEFAULT_KNOWLEDGE_LIMITS.maxKbs,
      topKMax: options?.top_k_max ?? DEFAULT_KNOWLEDGE_LIMITS.topKMax,
      waitMaxSec: options?.index_wait_max_sec ?? DEFAULT_KNOWLEDGE_LIMITS.waitMaxSec,
    }),
    [options],
  );
  const schema = useMemo(() => knowledgeIndexSchema(tc, t, limits), [tc, t, limits]);
  const form = useNodeForm<FormValues>(node, schema, knowledgeIndexToForm, knowledgeIndexFromForm);
  const serverIssue = useServerIssue(node.id);
  const issues = useNodeIssues(node.id);
  const metadata = useFieldArray({ control: form.control, name: "metadata" });
  const kbId = useWatch({ control: form.control, name: "kb_id" });
  const sourceKind = useWatch({ control: form.control, name: "source_kind" });
  const customRule = useWatch({ control: form.control, name: "custom_rule" });
  const rule = useWatch({ control: form.control, name: "rule" });
  const wait = useWatch({ control: form.control, name: "wait" });
  const kb = kbList.data?.find((item) => item.id === kbId);
  const fieldLists = useMetadataFieldLists(kb ? [kb.id] : []);
  // 기본 필드는 시스템이 채운다. 노드는 사용자 필드에만 값을 넣는다.
  const userFields = (fieldLists.lists?.[0] ?? []).filter((field) => !field.built_in);
  const policies = (options?.duplicate_policies ?? DUPLICATE_POLICIES).filter(
    (policy): policy is DuplicatePolicy =>
      (DUPLICATE_POLICIES as readonly string[]).includes(policy),
  );
  const paidWarning = issues.find((issue) => issue.code === "paid_key_required");
  const paid = kb?.data_policy.key_tier === "paid";

  const kbOptions: SelectOption[] = [
    { value: PICK, label: t("forms.knowledgeIndex.pickKb"), disabled: true },
    ...(kbList.data ?? []).map((item) => ({
      value: item.id,
      label: item.name,
      hint:
        item.data_policy.key_tier === "paid"
          ? t("forms.knowledge.paidBadge")
          : t("forms.knowledgeIndex.kbHint", { count: item.document_count }),
    })),
  ];
  if (kbId && !kb && kbList.isSuccess) {
    kbOptions.push({
      value: kbId,
      label: t("forms.knowledge.missingKb", { id: kbId.slice(0, 8) }),
    });
  }

  const sourceError =
    errorAt(form, sourceKind === "text" ? "text_var" : "files_var", serverIssue) ??
    serverIssue(sourceKind === "text" ? "source.text_var" : "source.files_var") ??
    serverIssue("source");

  return (
    <div className="flex flex-col gap-4">
      {kbList.isError ? (
        <ErrorAlert
          error={kbList.error}
          title={t("forms.knowledge.loadFailed")}
          onRetry={() => void kbList.refetch()}
        />
      ) : null}
      <Field
        label={t("forms.knowledgeIndex.kb")}
        error={errorAt(form, "kb_id", serverIssue)}
        required
      >
        <Controller
          control={form.control}
          name="kb_id"
          render={({ field }) => (
            <Select
              value={field.value || PICK}
              onValueChange={(value) => {
                field.onChange(value);
                // 다른 지식 베이스의 필드·청크 모드는 맞지 않을 수 있어 지운다.
                form.setValue("metadata", [], { shouldDirty: true });
                form.setValue("custom_rule", false, { shouldDirty: true });
              }}
              options={kbOptions}
            />
          )}
        />
      </Field>
      {paid ? (
        <Alert tone="warn" title={t("forms.knowledge.paidTitle")} className="text-xs">
          {options && !options.paid_key_configured
            ? t("forms.knowledge.paidMissing")
            : (paidWarning?.message ?? t("forms.knowledge.paidHint"))}
        </Alert>
      ) : null}
      <Section title={t("forms.knowledgeIndex.source")} hint={t("forms.knowledgeIndex.sourceHint")}>
        <div className="grid grid-cols-[9rem_minmax(0,1fr)] gap-2">
          <Controller
            control={form.control}
            name="source_kind"
            render={({ field }) => (
              <Select
                aria-label={t("forms.knowledgeIndex.sourceKind")}
                value={field.value}
                onValueChange={field.onChange}
                options={(["text", "files"] as const).map((value) => ({
                  value,
                  label: t(`forms.knowledgeIndex.sourceKinds.${value}`),
                }))}
              />
            )}
          />
          {sourceKind === "text" ? (
            <Controller
              control={form.control}
              name="text_var"
              render={({ field }) => (
                <VariablePicker
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  accept={["string"]}
                  aria-label={t("forms.knowledgeIndex.sourceKinds.text")}
                />
              )}
            />
          ) : (
            <Controller
              control={form.control}
              name="files_var"
              render={({ field }) => (
                <VariablePicker
                  nodeId={node.id}
                  value={field.value}
                  onChange={field.onChange}
                  accept={["file", "array[file]"]}
                  aria-label={t("forms.knowledgeIndex.sourceKinds.files")}
                />
              )}
            />
          )}
        </div>
        <FieldMessage message={sourceError} />
      </Section>
      <Field
        label={t("forms.knowledgeIndex.name")}
        hint={t("forms.knowledgeIndex.nameHint", { now: NOW_TEMPLATE })}
        error={serverIssue("name")}
      >
        <Controller
          control={form.control}
          name="name"
          render={({ field }) => (
            <PromptEditor
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              ariaLabel={t("forms.knowledgeIndex.name")}
              minHeight={36}
            />
          )}
        />
      </Field>
      <Section title={t("forms.knowledgeIndex.rule")} hint={t("forms.knowledgeIndex.ruleHint")}>
        <Field inline label={t("forms.knowledgeIndex.customRule")}>
          <Controller
            control={form.control}
            name="custom_rule"
            render={({ field }) => (
              <Switch
                checked={field.value}
                disabled={!kb}
                onCheckedChange={(on) => {
                  // 켜면 그 지식 베이스의 규칙에서 시작한다.
                  if (on && kb) {
                    form.setValue("rule", completeRule(kb.process_rule), { shouldDirty: true });
                  }
                  field.onChange(on);
                }}
              />
            )}
          />
        </Field>
        {customRule && kb ? (
          <ProcessRuleForm
            value={rule}
            onChange={(next) => form.setValue("rule", next, { shouldDirty: true })}
            docForm={kb.doc_form}
            errors={ruleErrors(rule, kb.doc_form)}
          />
        ) : null}
      </Section>
      <Section
        title={t("forms.knowledgeIndex.metadata")}
        hint={t("forms.knowledgeIndex.metadataHint")}
        actions={
          <AddButton
            label={t("forms.knowledgeIndex.addMetadata")}
            onClick={() =>
              metadata.append({
                name:
                  userFields.find((field) =>
                    form.getValues("metadata").every((item) => item.name !== field.name),
                  )?.name ?? "",
                value: "",
              })
            }
          />
        }
      >
        {metadata.fields.length === 0 ? (
          <EmptyRows>{t("forms.knowledgeIndex.noMetadata")}</EmptyRows>
        ) : null}
        {metadata.fields.map((row, index) => {
          const label = t("forms.knowledgeIndex.metadataRow", { n: index + 1 });
          const options: SelectOption[] = userFields.map((field) => ({
            value: field.name,
            label: field.name,
          }));
          const current = form.getValues(`metadata.${index}.name`);
          if (current && !options.some((option) => option.value === current)) {
            options.push({ value: current, label: current });
          }
          return (
            <div key={row.id} className="flex flex-col gap-1">
              <div className="grid grid-cols-[9rem_minmax(0,1fr)_auto] items-start gap-1.5">
                <Controller
                  control={form.control}
                  name={`metadata.${index}.name`}
                  render={({ field }) => (
                    <Select
                      aria-label={`${label} ${t("forms.knowledgeIndex.metadataName")}`}
                      className="h-8 text-xs"
                      value={field.value || undefined}
                      placeholder={t("forms.knowledgeIndex.metadataPick")}
                      onValueChange={field.onChange}
                      options={options}
                    />
                  )}
                />
                <Controller
                  control={form.control}
                  name={`metadata.${index}.value`}
                  render={({ field }) => (
                    <PromptEditor
                      nodeId={node.id}
                      value={field.value}
                      onChange={field.onChange}
                      ariaLabel={`${label} ${t("forms.knowledgeIndex.metadataValue")}`}
                      minHeight={32}
                    />
                  )}
                />
                <RemoveButton
                  label={t("forms.knowledgeIndex.removeMetadata", { n: index + 1 })}
                  onClick={() => metadata.remove(index)}
                />
              </div>
              <FieldMessage message={errorAt(form, `metadata.${index}.name`, serverIssue)} />
            </div>
          );
        })}
      </Section>
      <Field label={t("forms.knowledgeIndex.duplicate")} error={serverIssue("on_duplicate")}>
        <Controller
          control={form.control}
          name="on_duplicate"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={policies.map((policy) => ({
                value: policy,
                label: t(`forms.knowledgeIndex.duplicates.${policy}`),
                hint: t(`forms.knowledgeIndex.duplicateHints.${policy}`),
              }))}
            />
          )}
        />
      </Field>
      <Section title={t("forms.knowledgeIndex.wait")} hint={t("forms.knowledgeIndex.waitHint")}>
        <Field inline label={t("forms.knowledgeIndex.waitEnabled")}>
          <Controller
            control={form.control}
            name="wait"
            render={({ field }) => (
              <Switch checked={field.value} onCheckedChange={field.onChange} />
            )}
          />
        </Field>
        {wait ? (
          <>
            <Field
              label={t("forms.knowledgeIndex.timeout")}
              hint={t("forms.knowledgeIndex.timeoutHint", { max: limits.waitMaxSec })}
              error={errorAt(form, "timeout_sec", serverIssue)}
            >
              <Input
                type="number"
                step="1"
                className="w-32"
                {...form.register("timeout_sec", { setValueAs: toNumber })}
              />
            </Field>
            <p className="text-xs text-ink-muted">{t("forms.knowledgeIndex.waitStop")}</p>
          </>
        ) : null}
      </Section>
    </div>
  );
}
