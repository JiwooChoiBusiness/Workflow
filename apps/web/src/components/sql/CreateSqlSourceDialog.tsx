import { ChevronRight } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type { KeyTier, SqlApproval, SqlSourceKind } from "@/api/types";
import { DataPolicyNotice } from "@/components/knowledge/DataPolicyNotice";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { RadioCards } from "@/components/ui/radio-cards";
import { toast } from "@/components/ui/toast";
import { useCreateSqlSource } from "@/features/sql/api";
import "@/features/sql/i18n";
import {
  defaultLimits,
  limitsDraft,
  parseLimits,
  sameLimits,
  type LimitKey,
  type LimitProblem,
  type LimitsDraft,
} from "@/features/sql/limits";
import { useSqlError } from "@/features/sql/useSqlError";
import { useKbOptions, useSqlOptions } from "@/features/workflows/api";
import { cn } from "@/lib/cn";
import { SqlLimitsFields } from "./SqlLimitsFields";

const NAME_MAX = 40;
const DESCRIPTION_MAX = 400;
const KINDS: readonly SqlSourceKind[] = ["files", "sqlite"];
const APPROVALS: readonly SqlApproval[] = ["none", "always"];

interface Errors {
  name?: string;
  description?: string;
  policy?: string;
  root?: string;
}

/*
 * 데이터 소스 만들기(설계 10.2, 12.3): 이름·설명·종류·노드의 승인·한도와 데이터 정책 확인. 유료 키가 없으면
 * 유료 키 전용을 고를 수 없다. 한도를 건드리지 않으면 서버가 설정값(SQL_MAX_ROWS 등)으로 정한다.
 */
export function CreateSqlSourceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const create = useCreateSqlSource();
  const errorText = useSqlError();
  const options = useSqlOptions().data ?? null;
  const paidAvailable = useKbOptions().data?.paid_key_configured ?? false;
  const initialLimits = defaultLimits(options);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<SqlSourceKind>("files");
  const [approval, setApproval] = useState<SqlApproval>("none");
  const [limits, setLimits] = useState<LimitsDraft | null>(null);
  const [limitProblems, setLimitProblems] = useState<Partial<Record<LimitKey, LimitProblem>>>({});
  const [limitsOpen, setLimitsOpen] = useState(false);
  const [tier, setTier] = useState<KeyTier>("free");
  const [acknowledged, setAcknowledged] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const limitValues = limits ?? limitsDraft(initialLimits);

  function reset() {
    setName("");
    setDescription("");
    setKind("files");
    setApproval("none");
    setLimits(null);
    setLimitProblems({});
    setLimitsOpen(false);
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
    if (!acknowledged) found.policy = t("errors.policyRequired");
    const parsed = parseLimits(limitValues);
    setLimitProblems(parsed.problems);
    if (!parsed.limits) setLimitsOpen(true);
    setErrors(found);
    if (Object.keys(found).length > 0 || !parsed.limits) return;
    try {
      const source = await create.mutateAsync({
        name: trimmed,
        description: description.trim(),
        kind,
        approval,
        // 기본값 그대로면 보내지 않고 서버 설정값(SQL_MAX_ROWS·SQL_TIMEOUT_SEC·SQL_MEMORY_MB)을 쓰게 한다.
        limits: sameLimits(parsed.limits, initialLimits) ? null : parsed.limits,
        glossary: "",
        data_policy: { key_tier: tier, acknowledged },
      });
      toast.success(t("create.created"));
      onOpenChange(false);
      reset();
      navigate(`/knowledge/sql-sources/${source.id}`);
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
          <Field label={t("create.name")} error={errors.name} required>
            <Input
              value={name}
              maxLength={NAME_MAX + 10}
              onChange={(event) => setName(event.target.value)}
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
          <Field label={t("create.kind")} hint={t("create.kindFixed")}>
            <RadioCards
              ariaLabel={t("create.kind")}
              value={kind}
              onValueChange={(value) => setKind(value as SqlSourceKind)}
              options={KINDS.map((value) => ({
                value,
                label: t(`kind.${value}`),
                description: t(`kindHint.${value}`),
              }))}
            />
          </Field>
          <Field label={t("create.approval")}>
            <RadioCards
              ariaLabel={t("create.approval")}
              value={approval}
              onValueChange={(value) => setApproval(value as SqlApproval)}
              options={APPROVALS.map((value) => ({
                value,
                label: t(`approval.${value}`),
                description: t(`approvalHint.${value}`),
              }))}
            />
          </Field>
          <Collapsible.Root open={limitsOpen} onOpenChange={setLimitsOpen}>
            <Collapsible.Trigger className="flex items-center gap-1.5 text-sm font-medium text-ink">
              <ChevronRight
                className={cn(
                  "size-3.5 text-ink-subtle transition-transform",
                  limitsOpen && "rotate-90",
                )}
                aria-hidden
              />
              {t("create.limits")}
            </Collapsible.Trigger>
            <Collapsible.Content className="flex flex-col gap-3 pt-2">
              <p className="text-xs text-ink-muted">{t("create.limitsHint")}</p>
              <SqlLimitsFields
                value={limitValues}
                onChange={setLimits}
                problems={limitProblems}
                maxRowsCap={options?.max_rows_max ?? 1000}
              />
            </Collapsible.Content>
          </Collapsible.Root>
          <DataPolicyNotice
            subject="sql"
            tier={tier}
            onTierChange={setTier}
            acknowledged={acknowledged}
            onAcknowledgedChange={(value) => {
              setAcknowledged(value);
              if (value) setErrors((current) => ({ ...current, policy: undefined }));
            }}
            paidAvailable={paidAvailable}
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
