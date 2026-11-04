import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type { DocForm, IndexingTechnique, KeyTier, KnowledgeDefaults } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { RadioCards } from "@/components/ui/radio-cards";
import { Select } from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { useCreateKnowledgeBase } from "@/features/knowledge/api";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { DataPolicyNotice } from "./DataPolicyNotice";
import { IndexUnavailableNotice } from "./IndexUnavailableNotice";

const DOC_FORMS: readonly DocForm[] = ["text_model", "hierarchical_model", "qa_model"];
const NAME_MAX = 40;
const DESCRIPTION_MAX = 400;

interface Errors {
  name?: string;
  description?: string;
  policy?: string;
  root?: string;
}

// 지식 베이스 만들기(설계 12.3). 데이터 정책을 확인해야 만들 수 있고, 유료 키가 없으면 유료 키 전용을 고를 수 없다.
export function CreateKbDialog({
  open,
  onOpenChange,
  defaults,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaults: KnowledgeDefaults | undefined;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const create = useCreateKnowledgeBase();
  const errorText = useKnowledgeError();
  const model = defaults?.embedding_models[0];
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [docForm, setDocForm] = useState<DocForm>("text_model");
  const [technique, setTechnique] = useState<IndexingTechnique>("high_quality");
  const [dimensions, setDimensions] = useState<number | null>(null);
  const [tier, setTier] = useState<KeyTier>("free");
  const [acknowledged, setAcknowledged] = useState(false);
  const [errors, setErrors] = useState<Errors>({});

  const chosenDimensions = dimensions ?? model?.default_dimensions ?? 768;
  const dimensionOptions: readonly (768 | 1536 | 3072)[] = model?.dimensions ?? [768, 1536, 3072];

  function reset() {
    setName("");
    setDescription("");
    setDocForm("text_model");
    setTechnique("high_quality");
    setDimensions(null);
    setTier("free");
    setAcknowledged(false);
    setErrors({});
  }

  async function submit() {
    const found: Errors = {};
    const trimmed = name.trim();
    if (!trimmed) found.name = tc("validation.required");
    else if (trimmed.length > NAME_MAX) found.name = tc("validation.maxLength", { max: NAME_MAX });
    if (description.trim().length > DESCRIPTION_MAX) {
      found.description = tc("validation.maxLength", { max: DESCRIPTION_MAX });
    }
    if (!acknowledged) found.policy = t("policy.required");
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    try {
      const kb = await create.mutateAsync({
        name: trimmed,
        description: description.trim(),
        doc_form: docForm,
        indexing_technique: technique,
        embedding:
          technique === "high_quality"
            ? {
                model: model?.id ?? "gemini-embedding-2",
                dimensions: chosenDimensions as 768 | 1536 | 3072,
                prefix: model?.default_prefix ?? "gemini2",
              }
            : null,
        search_while_indexing: true,
        data_policy: { key_tier: tier, acknowledged },
      });
      toast.success(t("create.created"));
      onOpenChange(false);
      reset();
      navigate(`/knowledge/${kb.id}`);
    } catch (error) {
      setErrors({ root: errorText(error) });
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) reset();
      }}
    >
      <DialogContent title={t("create.title")} className="w-[min(40rem,calc(100vw-2rem))]">
        <form
          className="flex flex-col gap-4"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {defaults && !defaults.index_available ? (
            <IndexUnavailableNotice reason={defaults.index_error} />
          ) : null}
          <Field label={t("create.name")} error={errors.name} required>
            <Input
              value={name}
              maxLength={NAME_MAX + 10}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label={t("create.description")} error={errors.description}>
            <Textarea
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
          <Field label={t("create.docForm")} hint={t("create.docFormFixed")}>
            <RadioCards
              ariaLabel={t("create.docForm")}
              value={docForm}
              onValueChange={(value) => setDocForm(value as DocForm)}
              options={DOC_FORMS.map((value) => ({
                value,
                label: t(`docForm.${value}`),
                description: t(`docFormHint.${value}`),
              }))}
            />
          </Field>
          <Field label={t("create.technique")}>
            <RadioCards
              ariaLabel={t("create.technique")}
              value={technique}
              onValueChange={(value) => setTechnique(value as IndexingTechnique)}
              options={(["high_quality", "economy"] as const).map((value) => ({
                value,
                label: t(`technique.${value}`),
                description: t(`techniqueHint.${value}`),
              }))}
            />
          </Field>
          {technique === "high_quality" ? (
            <div className="grid grid-cols-2 gap-3">
              {/* 임베딩 모델은 지금 하나뿐이라 고르는 칸 대신 이름만 보인다. */}
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-ink">{t("create.embeddingModel")}</span>
                <span className="flex h-8 items-center font-mono text-xs text-ink">
                  {model?.id ?? "gemini-embedding-2"}
                </span>
              </div>
              <Field label={t("create.dimensions")}>
                <Select
                  value={String(chosenDimensions)}
                  onValueChange={(value) => setDimensions(Number(value))}
                  options={dimensionOptions.map((value) => ({
                    value: String(value),
                    label: t(`dimensions.${value}`),
                  }))}
                />
              </Field>
            </div>
          ) : null}
          <DataPolicyNotice
            tier={tier}
            onTierChange={setTier}
            acknowledged={acknowledged}
            onAcknowledgedChange={(value) => {
              setAcknowledged(value);
              if (value) setErrors((current) => ({ ...current, policy: undefined }));
            }}
            paidAvailable={defaults?.paid_key ?? false}
            error={errors.policy}
          />
          {errors.root ? <Alert tone="danger" title={errors.root} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              {t("create.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
