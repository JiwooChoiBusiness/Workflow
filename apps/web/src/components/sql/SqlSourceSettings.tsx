import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type { KeyTier, SqlApproval, SqlSourceOut, SqlSourcePatch } from "@/api/types";
import { DataPolicyNotice } from "@/components/knowledge/DataPolicyNotice";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { RadioCards } from "@/components/ui/radio-cards";
import { toast } from "@/components/ui/toast";
import { useDeleteSqlSource, usePatchSqlSource } from "@/features/sql/api";
import {
  limitsDraft,
  parseLimits,
  sameLimits,
  type LimitKey,
  type LimitProblem,
  type LimitsDraft,
} from "@/features/sql/limits";
import { useSqlError } from "@/features/sql/useSqlError";
import { useKbOptions, useSqlOptions } from "@/features/workflows/api";
import { SLUG_PATTERN } from "@/lib/formSchemas";
import { SqlLimitsFields } from "./SqlLimitsFields";

const NAME_MAX = 40;
const DESCRIPTION_MAX = 400;
const APPROVALS: readonly SqlApproval[] = ["none", "always"];

interface Errors {
  name?: string;
  slug?: string;
  description?: string;
  policy?: string;
}

/*
 * 데이터 소스 설정 탭: 이름·slug·설명, 노드의 승인, 한도, 데이터 정책(키 등급을 바꾸면 다시 확인), 삭제. 바뀐 칸만
 * PATCH로 보낸다. 종류(files·sqlite)는 바꿀 수 없다.
 */
export function SqlSourceSettings({ source }: { source: SqlSourceOut }) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const patch = usePatchSqlSource(source.id);
  const remove = useDeleteSqlSource();
  const errorText = useSqlError();
  const options = useSqlOptions().data ?? null;
  const paidAvailable = useKbOptions().data?.paid_key_configured ?? false;
  const [name, setName] = useState(source.name);
  const [slug, setSlug] = useState(source.slug);
  const [description, setDescription] = useState(source.description);
  const [approval, setApproval] = useState<SqlApproval>(source.approval);
  const [limits, setLimits] = useState<LimitsDraft>(() => limitsDraft(source.limits));
  const [limitProblems, setLimitProblems] = useState<Partial<Record<LimitKey, LimitProblem>>>({});
  const [tier, setTier] = useState<KeyTier>(source.data_policy.key_tier);
  const [acknowledged, setAcknowledged] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [deleting, setDeleting] = useState(false);
  const tierChanged = tier !== source.data_policy.key_tier;

  // 칸을 저장된 값으로 다시 채운다. 다른 탭에서 준비하거나 저장해 updated_at이 바뀌어도 고치던 칸은 두고,
  // 여기서 저장에 성공했을 때만 부른다.
  function reset(saved: SqlSourceOut) {
    setName(saved.name);
    setSlug(saved.slug);
    setDescription(saved.description);
    setApproval(saved.approval);
    setLimits(limitsDraft(saved.limits));
    setLimitProblems({});
    setTier(saved.data_policy.key_tier);
    setAcknowledged(false);
    setErrors({});
  }

  function save() {
    const found: Errors = {};
    const trimmed = name.trim();
    if (!trimmed) found.name = tc("validation.required");
    else if (trimmed.length > NAME_MAX) found.name = tc("validation.maxLength", { max: NAME_MAX });
    if (!SLUG_PATTERN.test(slug.trim())) found.slug = tc("validation.slug");
    if (description.trim().length > DESCRIPTION_MAX) {
      found.description = tc("validation.maxLength", { max: DESCRIPTION_MAX });
    }
    if (tierChanged && !acknowledged) found.policy = t("errors.policyRequired");
    const parsed = parseLimits(limits);
    setLimitProblems(parsed.problems);
    setErrors(found);
    if (Object.keys(found).length > 0 || !parsed.limits) return;
    const body: SqlSourcePatch = {};
    if (trimmed !== source.name) body.name = trimmed;
    if (slug.trim() !== source.slug) body.slug = slug.trim();
    if (description.trim() !== source.description) body.description = description.trim();
    if (approval !== source.approval) body.approval = approval;
    if (!sameLimits(parsed.limits, source.limits)) body.limits = parsed.limits;
    if (tierChanged) body.data_policy = { key_tier: tier, acknowledged };
    if (Object.keys(body).length === 0) return;
    patch.mutate(body, {
      onSuccess: (saved) => {
        reset(saved);
        toast.success(t("settings.saved"));
      },
      onError: (error) => toast.error(errorText(error)),
    });
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel title={t("settings.basics")}>
        <div className="flex flex-col gap-4">
          <Field label={t("create.name")} error={errors.name} required>
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field
            label={t("settings.slug")}
            hint={t("settings.slugHint")}
            error={errors.slug}
            required
          >
            <Input
              className="font-mono"
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
            />
          </Field>
          <Field
            label={t("create.description")}
            hint={t("create.descriptionHint")}
            error={errors.description}
          >
            <Textarea
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
        </div>
      </Panel>
      <Panel title={t("settings.approval")}>
        <RadioCards
          ariaLabel={t("settings.approval")}
          value={approval}
          onValueChange={(value) => setApproval(value as SqlApproval)}
          options={APPROVALS.map((value) => ({
            value,
            label: t(`approval.${value}`),
            description: t(`approvalHint.${value}`),
          }))}
        />
      </Panel>
      <Panel title={t("settings.limits")} description={t("create.limitsHint")}>
        <SqlLimitsFields
          value={limits}
          onChange={setLimits}
          problems={limitProblems}
          maxRowsCap={options?.max_rows_max ?? 1000}
        />
      </Panel>
      <Panel
        title={t("settings.policy")}
        description={tierChanged ? t("settings.policyAgain") : undefined}
      >
        <DataPolicyNotice
          subject="sql"
          tier={tier}
          onTierChange={setTier}
          acknowledged={tierChanged ? acknowledged : true}
          onAcknowledgedChange={setAcknowledged}
          paidAvailable={paidAvailable}
          error={errors.policy}
          acknowledgedAt={source.data_policy.acknowledged_at}
        />
      </Panel>
      <div className="flex justify-end">
        <Button variant="primary" loading={patch.isPending} onClick={save}>
          {t("settings.save")}
        </Button>
      </div>
      <Panel title={t("settings.danger")} className="border-danger/40">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted">{t("settings.deleteHint")}</p>
          <Button variant="danger" onClick={() => setDeleting(true)}>
            {t("settings.delete")}
          </Button>
        </div>
      </Panel>
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={t("list.deleteTitle")}
        description={t("list.deleteBody", { name: source.name })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(source.id, {
            onSuccess: () => {
              toast.success(t("list.deleted"));
              navigate("/knowledge/sql-sources");
            },
            onError: (error) => toast.error(errorText(error)),
          })
        }
      />
    </div>
  );
}
