import { ShieldAlert } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";
import type { KeyTier } from "@/api/types";
import { Label } from "@/components/ui/field";
import { RadioCards } from "@/components/ui/radio-cards";
import { Checkbox } from "@/components/ui/toggle";
import { formatDateTime } from "@/lib/format";

/*
 * KB·데이터 소스를 만들거나 키 등급을 바꿀 때 보이는 데이터 정책 안내(설계 14.4)와 확인. 유료 키가 없으면 paid를
 * 고를 수 없다. 데이터 소스(subject="sql")는 결과 표의 일부가 채팅 모델로 간다는 문장을 더한다. 그 문구는 sql
 * 네임스페이스라 데이터 소스 화면이 features/sql/i18n을 불러 둔다.
 */
export function DataPolicyNotice({
  tier,
  onTierChange,
  acknowledged,
  onAcknowledgedChange,
  paidAvailable,
  error,
  acknowledgedAt,
  subject = "knowledge",
}: {
  tier: KeyTier;
  onTierChange: (tier: KeyTier) => void;
  acknowledged: boolean;
  onAcknowledgedChange: (acknowledged: boolean) => void;
  paidAvailable: boolean;
  error?: string;
  acknowledgedAt?: string | null;
  subject?: "knowledge" | "sql";
}) {
  const { t } = useTranslation("knowledge");
  const { t: ts } = useTranslation("sql");
  const sql = subject === "sql";
  const checkboxId = useId();
  return (
    <section
      aria-label={t("policy.title")}
      className="flex flex-col gap-3 rounded-md border border-warn/30 bg-warn-soft px-3 py-3"
    >
      <div className="flex gap-2.5">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
        <div className="flex flex-col gap-1.5 text-sm text-ink">
          <p className="font-medium">{t("policy.title")}</p>
          <p>{sql ? ts("policy.body") : t("policy.body")}</p>
          {sql ? <p>{ts("policy.results")}</p> : null}
          <p className="text-ink-muted">{sql ? ts("policy.paid") : t("policy.paid")}</p>
          {sql ? null : <p className="text-ink-muted">{t("policy.secrets")}</p>}
          {acknowledgedAt ? (
            <p className="text-xs text-ink-muted">
              {t("policy.acknowledgedAt", { time: formatDateTime(acknowledgedAt) })}
            </p>
          ) : null}
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-ink">{t("policy.keyTier")}</span>
        <RadioCards
          ariaLabel={t("policy.keyTier")}
          value={tier}
          onValueChange={(value) => onTierChange(value as KeyTier)}
          options={[
            {
              value: "free",
              label: t("tier.free"),
              description: sql ? ts("policy.tierFree") : t("tierHint.free"),
            },
            {
              value: "paid",
              label: t("tier.paid"),
              description: !paidAvailable
                ? t("tierHint.paidMissing")
                : sql
                  ? ts("policy.tierPaid")
                  : t("tierHint.paid"),
              disabled: !paidAvailable,
            },
          ]}
        />
      </div>
      <div className="flex items-start gap-2">
        <Checkbox
          id={checkboxId}
          checked={acknowledged}
          aria-invalid={error ? true : undefined}
          onCheckedChange={(checked) => onAcknowledgedChange(checked === true)}
        />
        <Label htmlFor={checkboxId} className="font-normal">
          {sql ? ts("policy.acknowledge") : t("policy.acknowledge")}
        </Label>
      </div>
      {error ? (
        <p className="text-xs text-danger" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
