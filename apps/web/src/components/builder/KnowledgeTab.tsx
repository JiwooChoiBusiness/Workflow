import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { AgentWarning } from "@/api/types";
import { KbChecklist } from "@/components/knowledge/KbChecklist";
import { RetrievalSettingsForm } from "@/components/knowledge/RetrievalSettingsForm";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Panel } from "@/components/ui/layout";
import { Switch } from "@/components/ui/toggle";
import { DEFAULT_TOP_K_MAX, MAX_AGENT_KBS, type AgentForm } from "@/features/agents/schemas";
import { useKnowledgeBases, useKnowledgeDefaults } from "@/features/knowledge/api";
import "@/features/knowledge/i18n";
import { retrievalSummary } from "@/features/knowledge/messages";
import {
  DEFAULT_RETRIEVAL,
  toRetrievalForm,
  type RetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { SqlSourcesTab } from "./SqlSourcesTab";

/*
 * 에이전트 빌더의 지식 탭(3단계 설계 8.4, 10.3, 12.3). 지식 베이스를 5개까지 고르고 검색 설정을 정한다. 붙이면
 * search_knowledge 도구가 저절로 생기고(고르는 도구가 아니다) 시스템 프롬프트에 지식 베이스 절이 들어간다.
 * 둘 이상이면 검색 설정이 필요하다(저장은 경고, 발행은 거부). 아래 묶음에서 데이터 소스를 붙인다.
 */
export function KnowledgeTab({
  form,
  warnings,
}: {
  form: UseFormReturn<AgentForm>;
  // 마지막 초안 저장 응답의 지식 경고
  warnings: readonly AgentWarning[];
}) {
  const { t } = useTranslation("agents");
  const { t: tk } = useTranslation("knowledge");
  const kbList = useKnowledgeBases();
  const defaults = useKnowledgeDefaults();
  const kbIds = useWatch({ control: form.control, name: "knowledge.kb_ids" });
  const custom = useWatch({ control: form.control, name: "knowledge.custom_retrieval" });
  const retrieval = useWatch({ control: form.control, name: "knowledge.retrieval" });
  const topKMax = defaults.data?.top_k_max ?? DEFAULT_TOP_K_MAX;
  const byId = new Map((kbList.data ?? []).map((kb) => [kb.id, kb]));
  const chosen = kbIds.flatMap((id) => {
    const kb = byId.get(id);
    return kb ? [kb] : [];
  });
  const many = new Set(kbIds).size > 1;
  const paid = chosen.filter((kb) => kb.data_policy.key_tier === "paid");

  function startCustom(ids: readonly string[]) {
    const first = ids.map((id) => byId.get(id)).find(Boolean);
    form.setValue(
      "knowledge.retrieval",
      toRetrievalForm(
        first?.retrieval_model ?? defaults.data?.retrieval_model ?? DEFAULT_RETRIEVAL,
      ),
      { shouldDirty: true },
    );
    form.setValue("knowledge.custom_retrieval", true, { shouldDirty: true });
  }

  function setKbs(next: string[]) {
    form.setValue("knowledge.kb_ids", next, { shouldDirty: true, shouldValidate: true });
    if (new Set(next).size > 1 && !form.getValues("knowledge.custom_retrieval")) startCustom(next);
  }

  const kbWarning = (kbId: string) =>
    warnings.find((warning) => warning.kb_id === kbId && warning.code !== "paid_key_required")
      ?.message;
  const errors = form.formState.errors.knowledge;
  const listError = errors?.kb_ids?.message;
  // 저장 때 서버가 돌려준 칸 오류만 칸에 보인다. 폼 스키마의 검색 설정 오류는 검색 설정 폼이 직접 셈한다.
  const retrievalIssue = (field: keyof RetrievalForm) => {
    const error = errors?.retrieval?.[field];
    return error?.type === "server" ? error.message : undefined;
  };

  return (
    <div className="flex flex-col gap-6">
      <Panel title={t("knowledge.title")} description={t("knowledge.description")}>
        <div className="flex flex-col gap-3">
          <KbChecklist
            idPrefix="agent-kb"
            kbs={kbList.data}
            loading={kbList.isPending}
            error={kbList.error}
            onRetry={() => void kbList.refetch()}
            selected={kbIds}
            onChange={setKbs}
            max={MAX_AGENT_KBS}
            issueFor={kbWarning}
          />
          {listError ? (
            <p className="text-xs text-danger" role="alert">
              {listError}
            </p>
          ) : null}
          {kbIds.length > 0 ? (
            <Alert tone="info" title={t("knowledge.toolAttached")}>
              {t("knowledge.toolAttachedHint")}
            </Alert>
          ) : null}
          {paid.length > 0 ? (
            <Alert tone="warn" title={t("knowledge.paidTitle")}>
              {defaults.data && !defaults.data.paid_key
                ? t("knowledge.paidMissing")
                : t("knowledge.paidHint", { names: paid.map((kb) => kb.name).join(", ") })}
            </Alert>
          ) : null}
        </div>
      </Panel>
      <Panel title={t("knowledge.retrieval")} description={t("knowledge.retrievalHint")}>
        <div className="flex flex-col gap-4">
          <Field
            inline
            label={t("knowledge.inherit")}
            hint={
              many
                ? t("knowledge.inheritMany")
                : !custom && chosen[0]
                  ? t("knowledge.inheritSummary", {
                      summary: retrievalSummary(chosen[0].retrieval_model, tk),
                    })
                  : undefined
            }
          >
            <Switch
              checked={!custom}
              disabled={kbIds.length === 0 || (many && custom)}
              onCheckedChange={(inherit) => {
                if (inherit) {
                  form.setValue("knowledge.custom_retrieval", false, { shouldDirty: true });
                } else {
                  startCustom(kbIds);
                }
              }}
            />
          </Field>
          {custom ? (
            <RetrievalSettingsForm
              value={retrieval}
              onChange={(next) =>
                form.setValue("knowledge.retrieval", next, {
                  shouldDirty: true,
                  shouldValidate: true,
                })
              }
              technique={
                chosen.some((kb) => kb.indexing_technique === "high_quality") || chosen.length === 0
                  ? "high_quality"
                  : "economy"
              }
              topKMax={topKMax}
              issueAt={retrievalIssue}
            />
          ) : null}
          {many && !custom ? (
            <p className="text-xs text-danger" role="alert">
              {t("knowledge.retrievalRequired")}
            </p>
          ) : errors?.custom_retrieval?.message ? (
            <p className="text-xs text-danger" role="alert">
              {errors.custom_retrieval.message}
            </p>
          ) : null}
        </div>
      </Panel>
      <SqlSourcesTab form={form} warnings={warnings} />
      {warnings.length > 0 ? (
        <section aria-label={t("knowledge.warnings")} className="flex flex-col gap-2">
          {warnings.map((warning, index) => (
            <Alert
              key={`${warning.code}:${warning.kb_id ?? warning.source_id ?? warning.agent_id ?? index}`}
              tone="warn"
              title={warning.message}
            >
              {warning.code === "paid_key_required" && warning.agent_id
                ? t("knowledge.subAgentPaid")
                : null}
            </Alert>
          ))}
        </section>
      ) : null}
    </div>
  );
}
