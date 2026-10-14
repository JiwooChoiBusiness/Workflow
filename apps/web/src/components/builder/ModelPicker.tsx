import { useState } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { ModelEntry, ModelList, ReasoningEffort } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, type SelectOption } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/toggle";
import { DEFAULT_MAX_OUTPUT_TOKENS } from "@/features/agents/schemas";
import { formatNumber } from "@/lib/format";
import { toNumber, toNumberOrNull } from "@/lib/formSchemas";

// Radix Select는 빈 값을 받지 않아 "모델 지정 안 함"과 "모델 기본값"을 표식 값으로 둔다.
const DEFAULT_MODEL = "__fallback__";
const DEFAULT_EFFORT = "__model_default__";

// model_id가 null이면 폴백 목록의 첫 모델이 실제로 쓰인다(설계 5.1).
export function effectiveModel(
  entries: ModelEntry[] | undefined,
  modelId: string | null,
): ModelEntry | undefined {
  if (!entries) return undefined;
  if (modelId === null) return entries.find((entry) => entry.fallback_index === 0);
  return entries.find((entry) => entry.id === modelId);
}

export function maxOutputTokensFor(entries: ModelEntry[] | undefined, modelId: string | null) {
  return (
    effectiveModel(entries, modelId)?.capabilities?.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS
  );
}

// 에이전트 버전의 model과 워크플로우 모델 노드(llm·분류·추출)의 model이 같은 모양이다.
export interface ModelFields {
  model: {
    model_id: string | null;
    params: {
      temperature: number | null;
      top_p: number | null;
      max_output_tokens: number;
      reasoning_effort: ReasoningEffort | null;
    };
  };
  // 에이전트 빌더만 있는 저장 옵션(allow_unlisted_model)
  allow_unlisted_model?: boolean;
}

export function ModelPicker<F extends ModelFields>({
  form: anyForm,
  models,
  modelsError,
  onRetry,
  unlistedOption = true,
}: {
  form: UseFormReturn<F>;
  models: ModelList | undefined;
  modelsError: unknown;
  onRetry: () => void;
  // 워크플로우 노드는 모델을 실행 때 어댑터가 확인하므로 "목록에 없어도 저장" 옵션이 없다.
  unlistedOption?: boolean;
}) {
  const { t } = useTranslation("agents");
  const form = anyForm as unknown as UseFormReturn<ModelFields>;
  const control = form.control;
  const modelId = useWatch({ control, name: "model.model_id" });
  const effort = useWatch({ control, name: "model.params.reasoning_effort" });
  const allowUnlisted = useWatch({ control, name: "allow_unlisted_model" });
  const [resetEffort, setResetEffort] = useState<ReasoningEffort | null>(null);
  const errors = form.formState.errors.model;

  const entries = models?.models;
  const selected = effectiveModel(entries, modelId);
  const capabilities = selected?.capabilities ?? null;
  const allowedEfforts = capabilities?.reasoning_efforts ?? [];
  const fallbackOrder = (entries ?? [])
    .filter((entry) => entry.fallback_index !== null && entry.fallback_index !== undefined)
    .map((entry) => entry.id)
    .join(" → ");

  const modelOptions: SelectOption[] = [
    { value: DEFAULT_MODEL, label: t("model.default"), hint: fallbackOrder || undefined },
    ...(entries ?? []).map((entry) => ({
      value: entry.id,
      label: entry.id,
      hint: modelHint(entry),
    })),
  ];
  // 저장된 모델이 지금 목록에 없어도 값이 사라지지 않게 항목으로 남긴다.
  if (modelId && !(entries ?? []).some((entry) => entry.id === modelId)) {
    modelOptions.push({ value: modelId, label: modelId, hint: t("model.notLive") });
  }

  function modelHint(entry: ModelEntry): string | undefined {
    const parts: string[] = [];
    if (entry.fallback_index !== null && entry.fallback_index !== undefined) {
      parts.push(t("model.fallbackRank", { rank: entry.fallback_index + 1 }));
    }
    if (entry.live === false) parts.push(t("model.notLive"));
    if (!entry.capabilities) parts.push(t("model.noCapabilities"));
    return parts.join(" · ") || undefined;
  }

  const effortOptions: SelectOption[] = [
    { value: DEFAULT_EFFORT, label: t("model.reasoningDefault") },
    ...allowedEfforts.map((value) => ({ value, label: t(`model.efforts.${value}`) })),
  ];

  function changeModel(value: string) {
    const nextId = value === DEFAULT_MODEL ? null : value;
    form.setValue("model.model_id", nextId, { shouldDirty: true });
    const nextAllowed = effectiveModel(entries, nextId)?.capabilities?.reasoning_efforts ?? [];
    if (effort && !nextAllowed.includes(effort)) {
      form.setValue("model.params.reasoning_effort", null, { shouldDirty: true });
      setResetEffort(effort);
    } else {
      setResetEffort(null);
    }
  }

  const inFallback = selected?.fallback_index !== null && selected?.fallback_index !== undefined;
  // 서버는 폴백 목록과 받아 둔 라이브 목록에 있는 모델만 받는다. 목록에서 확인되지 않으면 저장 옵션을 보인다.
  const unverified = unlistedOption && modelId !== null && !inFallback && selected?.live !== true;
  const register = form.register;

  return (
    <div className="flex flex-col gap-4">
      {modelsError ? (
        <ErrorAlert error={modelsError} title={t("model.loadFailed")} onRetry={onRetry} />
      ) : null}
      {models?.fake ? <Alert tone="info" title={t("model.fake")} /> : null}
      {models && !models.fake && !models.live_ok ? (
        <Alert tone="warn" title={t("model.liveFailed")} />
      ) : null}
      <Field
        label={t("model.label")}
        hint={
          modelId === null
            ? undefined
            : inFallback
              ? t("model.fallbackChain")
              : t("model.noFallback")
        }
      >
        <Select
          value={modelId ?? DEFAULT_MODEL}
          onValueChange={changeModel}
          options={modelOptions}
          disabled={!models}
        />
      </Field>
      {errors?.model_id?.message ? (
        <p className="-mt-2 text-xs text-danger" role="alert">
          {errors.model_id.message}
        </p>
      ) : null}
      {unverified || (unlistedOption && allowUnlisted) ? (
        <Field inline label={t("model.allowUnlisted")} hint={t("model.allowUnlistedHint")}>
          <Checkbox
            className="mt-0.5"
            checked={allowUnlisted}
            onCheckedChange={(value) =>
              form.setValue("allow_unlisted_model", value === true, { shouldDirty: true })
            }
          />
        </Field>
      ) : null}
      {capabilities ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          <Badge>{selected?.id}</Badge>
          {t("model.limits", {
            window: formatNumber(capabilities.context_window),
            output: formatNumber(capabilities.max_output_tokens),
          })}
        </div>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label={t("model.reasoningEffort")}
          hint={capabilities ? undefined : t("model.reasoningUnavailable")}
          error={errors?.params?.reasoning_effort?.message}
        >
          <Select
            value={effort ?? DEFAULT_EFFORT}
            onValueChange={(value) => {
              setResetEffort(null);
              form.setValue(
                "model.params.reasoning_effort",
                value === DEFAULT_EFFORT ? null : (value as ReasoningEffort),
                { shouldDirty: true },
              );
            }}
            options={effortOptions}
            disabled={!capabilities || allowedEfforts.length === 0}
          />
        </Field>
        <Field
          label={t("model.maxOutputTokens")}
          hint={t("model.maxOutputTokensHint", {
            max: formatNumber(capabilities?.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS),
          })}
          error={errors?.params?.max_output_tokens?.message}
        >
          <Input
            type="number"
            step="1"
            {...register("model.params.max_output_tokens", { setValueAs: toNumber })}
          />
        </Field>
        <Field
          label={t("model.temperature")}
          hint={t("model.unsetHint")}
          error={errors?.params?.temperature?.message}
        >
          <Input
            type="number"
            step="0.1"
            {...register("model.params.temperature", { setValueAs: toNumberOrNull })}
          />
        </Field>
        <Field
          label={t("model.topP")}
          hint={t("model.unsetHint")}
          error={errors?.params?.top_p?.message}
        >
          <Input
            type="number"
            step="0.05"
            {...register("model.params.top_p", { setValueAs: toNumberOrNull })}
          />
        </Field>
      </div>
      {resetEffort ? (
        <Alert
          tone="info"
          title={t("model.reasoningReset", { value: t(`model.efforts.${resetEffort}`) })}
        />
      ) : null}
    </div>
  );
}
