import { useTranslation } from "react-i18next";
import type { IndexingTechnique } from "@/api/types";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { RadioCards } from "@/components/ui/radio-cards";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import {
  methodsFor,
  RERANK_CANDIDATES_MAX,
  RERANKING_MODES,
  retrievalErrors,
  type RetrievalForm,
} from "@/features/knowledge/retrievalSettings";

function toNumber(raw: string): number {
  return raw === "" ? Number.NaN : Number(raw);
}

function shown(value: number): number | string {
  return Number.isNaN(value) ? "" : value;
}

// 검색 설정(설계 7장). KB 설정, 검색 시험의 임시 설정, 평가 실행이 같이 쓴다. 문턱은 코사인 원값에 건다(7.3).
export function RetrievalSettingsForm({
  value,
  onChange,
  technique,
  topKMax,
  candidatesMax = RERANK_CANDIDATES_MAX,
  issueAt,
  disabled,
}: {
  value: RetrievalForm;
  onChange: (next: RetrievalForm) => void;
  technique: IndexingTechnique;
  topKMax: number;
  // 노드 폼은 kb_options.rerank_candidates_max를 넘긴다.
  candidatesMax?: number;
  // 폼 밖에서 온 칸 오류(저장 때 서버 검증). 이 폼의 검사가 먼저다.
  issueAt?: (field: keyof RetrievalForm) => string | undefined;
  disabled?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const errors = retrievalErrors(value, topKMax, candidatesMax);
  const methods = methodsFor(technique);
  const set = (patch: Partial<RetrievalForm>) => onChange({ ...value, ...patch });
  const usesVector = technique === "high_quality" && value.search_method !== "full_text_search";
  const hybrid = technique === "high_quality" && value.search_method === "hybrid_search";
  const keyword =
    Math.round((1 - (Number.isNaN(value.vector_weight) ? 0 : value.vector_weight)) * 100) / 100;

  return (
    // 검색 시험 옆 칸처럼 좁은 곳에서도 쓰므로 화면 폭이 아니라 이 폼의 폭으로 칸을 나눈다.
    <div className="@container flex flex-col gap-4">
      <Field
        label={t("retrieval.method")}
        hint={technique === "economy" ? t("retrieval.economyOnly") : undefined}
        error={issueAt?.("search_method")}
      >
        <RadioCards
          ariaLabel={t("retrieval.method")}
          value={value.search_method}
          onValueChange={(method) =>
            set({ search_method: method as RetrievalForm["search_method"] })
          }
          className="@xl:grid @xl:grid-cols-3"
          options={(["hybrid_search", "semantic_search", "full_text_search"] as const).map(
            (method) => ({
              value: method,
              label: t(`retrieval.methods.${method}`),
              description: t(`retrieval.methodHints.${method}`),
              disabled: disabled || !methods.includes(method),
            }),
          )}
        />
      </Field>
      <div className="grid gap-3 @lg:grid-cols-2">
        <Field
          label={t("retrieval.topK")}
          hint={t("retrieval.topKHint", { max: topKMax })}
          error={errors.top_k ? t(`retrieval.errors.${errors.top_k}`) : issueAt?.("top_k")}
        >
          <Input
            type="number"
            min={1}
            max={topKMax}
            value={shown(value.top_k)}
            disabled={disabled}
            onChange={(event) => set({ top_k: toNumber(event.target.value) })}
          />
        </Field>
        {usesVector ? (
          <div className="flex flex-col gap-1.5">
            <Field label={t("retrieval.threshold")} inline>
              <Switch
                checked={value.score_threshold_enabled}
                disabled={disabled}
                onCheckedChange={(checked) => set({ score_threshold_enabled: checked })}
              />
            </Field>
            <Field
              hint={t("retrieval.thresholdHint")}
              error={
                errors.score_threshold
                  ? t(`retrieval.errors.${errors.score_threshold}`)
                  : issueAt?.("score_threshold")
              }
            >
              <Input
                type="number"
                step={0.05}
                min={0}
                max={1}
                aria-label={t("retrieval.threshold")}
                value={shown(value.score_threshold)}
                disabled={disabled || !value.score_threshold_enabled}
                onChange={(event) => set({ score_threshold: toNumber(event.target.value) })}
              />
            </Field>
          </div>
        ) : null}
      </div>
      {hybrid ? (
        <>
          <Field
            label={t("retrieval.reranking")}
            hint={t(`retrieval.modeHints.${value.reranking_mode}`)}
            error={issueAt?.("reranking_mode")}
          >
            <Select
              value={value.reranking_mode}
              disabled={disabled}
              onValueChange={(mode) =>
                set({ reranking_mode: mode as RetrievalForm["reranking_mode"] })
              }
              options={RERANKING_MODES.map((mode) => ({
                value: mode,
                label: t(`retrieval.modes.${mode}`),
              }))}
            />
          </Field>
          <Field
            label={t("retrieval.vectorWeight")}
            hint={t("retrieval.weightsValue", { vector: value.vector_weight, keyword })}
            error={
              errors.vector_weight
                ? t(`retrieval.errors.${errors.vector_weight}`)
                : issueAt?.("vector_weight")
            }
          >
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={Number.isNaN(value.vector_weight) ? 0.7 : value.vector_weight}
              aria-label={t("retrieval.vectorWeight")}
              aria-valuetext={t("retrieval.weightsValue", { vector: value.vector_weight, keyword })}
              disabled={disabled}
              className="w-full accent-[var(--color-accent)]"
              onChange={(event) => set({ vector_weight: Number(event.target.value) })}
            />
          </Field>
          {value.reranking_mode === "llm" ? (
            <div className="grid gap-3 @lg:grid-cols-2">
              <Field
                label={t("retrieval.candidates")}
                error={
                  errors.rerank_candidates
                    ? t(`retrieval.errors.${errors.rerank_candidates}`, { max: candidatesMax })
                    : issueAt?.("rerank_candidates")
                }
              >
                <Input
                  type="number"
                  min={1}
                  max={candidatesMax}
                  value={shown(value.rerank_candidates)}
                  disabled={disabled}
                  onChange={(event) => set({ rerank_candidates: toNumber(event.target.value) })}
                />
              </Field>
              <Field
                label={t("retrieval.rerankModel")}
                hint={t("retrieval.rerankModelHint")}
                error={issueAt?.("rerank_model_id")}
              >
                <Input
                  value={value.rerank_model_id}
                  disabled={disabled}
                  className="font-mono"
                  onChange={(event) => set({ rerank_model_id: event.target.value })}
                />
              </Field>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
