import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type {
  KeyTier,
  KnowledgeBaseOut,
  KnowledgeBasePatch,
  KnowledgeBaseStats,
  KnowledgeDefaults,
} from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { useDeleteKnowledgeBase, usePatchKnowledgeBase } from "@/features/knowledge/api";
import {
  completeRule,
  ruleErrors,
  sameRule,
  toProcessRule,
} from "@/features/knowledge/processRule";
import {
  fromRetrievalForm,
  retrievalFormValid,
  sameRetrieval,
  toRetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { SLUG_PATTERN } from "@/lib/formSchemas";
import { DataPolicyNotice } from "./DataPolicyNotice";
import { ProcessRuleForm } from "./ProcessRuleForm";
import { RetrievalSettingsForm } from "./RetrievalSettingsForm";

type Dimensions = 768 | 1536 | 3072;
type Prefix = "none" | "gemini2";

function SaveButton({
  dirty,
  pending,
  disabled,
  onClick,
}: {
  dirty: boolean;
  pending: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  const { t: tc } = useTranslation();
  return (
    <Button
      size="sm"
      variant="primary"
      disabled={!dirty || disabled}
      loading={pending}
      onClick={onClick}
    >
      {tc("actions.save")}
    </Button>
  );
}

// 설정 탭(설계 12.3): 이름·설명, 색인(경제 → 고품질, 임베딩 설정은 재임베딩 확인), 기본 규칙, 검색 설정, 데이터 정책,
// 위험 구역. 칸마다 따로 저장해 바뀐 것만 보낸다.
export function KbSettingsPanel({
  kb,
  stats,
  defaults,
}: {
  kb: KnowledgeBaseOut;
  stats: KnowledgeBaseStats | undefined;
  defaults: KnowledgeDefaults | undefined;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const errorText = useKnowledgeError();
  const patch = usePatchKnowledgeBase(kb.id);
  const remove = useDeleteKnowledgeBase();
  const topKMax = defaults?.top_k_max ?? 20;
  const model = defaults?.embedding_models[0];

  const [name, setName] = useState(kb.name);
  const [description, setDescription] = useState(kb.description);
  const [slug, setSlug] = useState(kb.slug);
  const [dimensions, setDimensions] = useState<Dimensions>(
    (kb.embedding?.dimensions as Dimensions | undefined) ?? model?.default_dimensions ?? 768,
  );
  const [prefix, setPrefix] = useState<Prefix>(kb.embedding?.prefix ?? "gemini2");
  const [rule, setRule] = useState(() => completeRule(kb.process_rule));
  const [retrieval, setRetrieval] = useState(() => toRetrievalForm(kb.retrieval_model));
  const [tier, setTier] = useState<KeyTier>(kb.data_policy.key_tier);
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirm, setConfirm] = useState<"reembed" | "upgrade" | "delete" | null>(null);
  const [problem, setProblem] = useState<{ section: string; message: string } | null>(null);

  const limits = defaults?.limiter[kb.data_policy.key_tier];
  const rate = limits ? Math.max(1, limits.rpm - limits.reserve) : 80;
  const units = stats?.unit_count ?? 0;
  const minutes = Math.ceil(units / rate);
  const embeddingDirty =
    kb.indexing_technique === "high_quality" &&
    (dimensions !== kb.embedding?.dimensions || prefix !== kb.embedding?.prefix);
  const ruleProblems = ruleErrors(rule, kb.doc_form);
  const slugValid = SLUG_PATTERN.test(slug.trim());

  // 성공 문구는 하나만 띄운다. 재임베딩·승격은 저장보다 뒤에 무엇이 일어나는지를 알린다.
  function save(
    section: string,
    body: Partial<KnowledgeBasePatch>,
    done?: () => void,
    message: string = t("settings.saved"),
  ) {
    setProblem(null);
    patch.mutate(body, {
      onSuccess: () => {
        toast.success(message);
        done?.();
      },
      onError: (error) => setProblem({ section, message: errorText(error) }),
    });
  }

  const sectionError = (section: string) =>
    problem?.section === section ? (
      <Alert tone="danger" title={problem.message} className="mt-3" />
    ) : null;

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={t("settings.basics")}
        actions={
          <SaveButton
            dirty={
              name.trim() !== kb.name ||
              description.trim() !== kb.description ||
              slug.trim() !== kb.slug
            }
            pending={patch.isPending}
            disabled={
              !name.trim() || name.trim().length > 40 || description.length > 400 || !slugValid
            }
            onClick={() =>
              save("basics", {
                name: name.trim(),
                description: description.trim(),
                ...(slug.trim() !== kb.slug ? { slug: slug.trim() } : {}),
              })
            }
          />
        }
      >
        <div className="flex flex-col gap-3">
          <Field label={t("settings.name")} required>
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={t("settings.description")}>
            <Textarea
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
          <Field
            label={t("settings.slug")}
            hint={t("settings.slugHint")}
            error={slugValid ? undefined : tc("validation.slug")}
          >
            <Input
              value={slug}
              className="font-mono"
              onChange={(event) => setSlug(event.target.value)}
            />
          </Field>
        </div>
        {sectionError("basics")}
      </Panel>

      <Panel
        title={t("settings.indexing")}
        description={t("settings.indexingHint", { docForm: t(`docForm.${kb.doc_form}`) })}
        actions={
          kb.indexing_technique === "economy" ? (
            <Button size="sm" onClick={() => setConfirm("upgrade")}>
              {t("settings.upgrade")}
            </Button>
          ) : (
            <SaveButton
              dirty={embeddingDirty}
              pending={patch.isPending}
              onClick={() => setConfirm("reembed")}
            />
          )
        }
      >
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-sm">
            <Badge tone={kb.indexing_technique === "high_quality" ? "accent" : "neutral"}>
              {t(`technique.${kb.indexing_technique}`)}
            </Badge>
            <span className="text-ink-muted">{t(`techniqueHint.${kb.indexing_technique}`)}</span>
          </div>
          {kb.indexing_technique === "high_quality" ? (
            <>
              <p className="text-xs text-ink-muted">{t("settings.embeddingHint")}</p>
              <div className="grid gap-3 sm:grid-cols-3">
                {/* 임베딩 모델은 지금 하나뿐이라 고르는 칸 대신 이름만 보인다. */}
                <div className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium text-ink">{t("create.embeddingModel")}</span>
                  <span className="flex h-8 items-center font-mono text-xs text-ink">
                    {kb.embedding?.model ?? model?.id ?? "gemini-embedding-2"}
                  </span>
                </div>
                <Field label={t("create.dimensions")}>
                  <Select
                    value={String(dimensions)}
                    onValueChange={(value) => setDimensions(Number(value) as Dimensions)}
                    options={(model?.dimensions ?? [768, 1536, 3072]).map((value) => ({
                      value: String(value),
                      label: t(`dimensions.${value as Dimensions}`),
                    }))}
                  />
                </Field>
                <Field label={t("prefix.label")}>
                  <Select
                    value={prefix}
                    onValueChange={(value) => setPrefix(value as Prefix)}
                    options={(["gemini2", "none"] as const).map((value) => ({
                      value,
                      label: t(`prefix.${value}`),
                    }))}
                  />
                </Field>
              </div>
            </>
          ) : null}
          <Field
            label={t("settings.searchWhileIndexing")}
            hint={t("settings.searchWhileIndexingHint")}
            inline
          >
            <Switch
              checked={kb.search_while_indexing}
              disabled={patch.isPending}
              onCheckedChange={(checked) => save("indexing", { search_while_indexing: checked })}
            />
          </Field>
        </div>
        {sectionError("indexing")}
      </Panel>

      <Panel
        title={t("settings.rules")}
        description={t("settings.rulesHint")}
        actions={
          <SaveButton
            dirty={!sameRule(toProcessRule(rule), kb.process_rule)}
            pending={patch.isPending}
            disabled={Object.keys(ruleProblems).length > 0}
            onClick={() => save("rules", { process_rule: toProcessRule(rule) })}
          />
        }
      >
        <ProcessRuleForm
          value={rule}
          onChange={setRule}
          docForm={kb.doc_form}
          errors={ruleProblems}
        />
        {sectionError("rules")}
      </Panel>

      <Panel
        title={t("settings.retrieval")}
        description={t("settings.retrievalHint")}
        actions={
          <SaveButton
            dirty={!sameRetrieval(fromRetrievalForm(retrieval), kb.retrieval_model)}
            pending={patch.isPending}
            disabled={!retrievalFormValid(retrieval, topKMax)}
            onClick={() => save("retrieval", { retrieval_model: fromRetrievalForm(retrieval) })}
          />
        }
      >
        <RetrievalSettingsForm
          value={retrieval}
          onChange={setRetrieval}
          technique={kb.indexing_technique}
          topKMax={topKMax}
        />
        {sectionError("retrieval")}
      </Panel>

      <Panel
        title={t("settings.policy")}
        actions={
          <SaveButton
            dirty={tier !== kb.data_policy.key_tier}
            pending={patch.isPending}
            disabled={!acknowledged}
            onClick={() =>
              save("policy", { data_policy: { key_tier: tier, acknowledged: true } }, () =>
                setAcknowledged(false),
              )
            }
          />
        }
      >
        <DataPolicyNotice
          tier={tier}
          onTierChange={setTier}
          acknowledged={acknowledged}
          onAcknowledgedChange={setAcknowledged}
          paidAvailable={defaults?.paid_key ?? false}
          acknowledgedAt={kb.data_policy.acknowledged_at}
        />
        {sectionError("policy")}
      </Panel>

      <Panel title={t("settings.danger")} className="border-danger/40">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted">{t("settings.deleteHint")}</p>
          <Button variant="danger" size="sm" onClick={() => setConfirm("delete")}>
            {t("settings.delete")}
          </Button>
        </div>
      </Panel>

      <ConfirmDialog
        open={confirm === "reembed"}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={t("settings.reembedTitle")}
        description={t("settings.reembedBody", { units, minutes })}
        confirmLabel={t("settings.reembedConfirm")}
        pending={patch.isPending}
        error={problem?.section === "reembed" ? problem.message : undefined}
        onConfirm={() =>
          save(
            "reembed",
            {
              embedding: {
                model: kb.embedding?.model ?? "gemini-embedding-2",
                dimensions,
                prefix,
              },
              confirm_reembed: true,
            },
            () => setConfirm(null),
            t("settings.reembedStarted"),
          )
        }
      />
      <ConfirmDialog
        open={confirm === "upgrade"}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        tone="default"
        title={t("settings.upgradeTitle")}
        description={t("settings.upgradeBody", { units, minutes })}
        confirmLabel={t("settings.upgrade")}
        pending={patch.isPending}
        error={problem?.section === "upgrade" ? problem.message : undefined}
        onConfirm={() =>
          save(
            "upgrade",
            { indexing_technique: "high_quality" },
            () => setConfirm(null),
            t("settings.upgraded"),
          )
        }
      />
      <ConfirmDialog
        open={confirm === "delete"}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={t("list.deleteTitle")}
        description={t("list.deleteBody", { name: kb.name })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(kb.id, {
            onSuccess: () => {
              toast.success(t("list.deleted"));
              navigate("/knowledge", { replace: true });
            },
            onError: (error) => toast.error(errorText(error)),
          })
        }
      />
    </div>
  );
}
