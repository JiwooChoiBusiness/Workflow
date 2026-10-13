import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { APPROVAL_MODES, type ApprovalMode } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { RadioCards } from "@/components/ui/radio-cards";
import type { AgentForm } from "@/features/agents/schemas";
import { toNumberOrNull } from "@/lib/formSchemas";
import { LimitsFields } from "./LimitsFields";

export function ApprovalModeSelect({
  value,
  onChange,
}: {
  value: ApprovalMode;
  onChange: (mode: ApprovalMode) => void;
}) {
  const { t } = useTranslation("agents");
  return (
    <div className="flex flex-col gap-3">
      <RadioCards
        ariaLabel={t("approval.title")}
        value={value}
        onValueChange={(mode) => onChange(mode as ApprovalMode)}
        options={APPROVAL_MODES.map((mode) => ({
          value: mode,
          label: t(`approval.modes.${mode}.label`),
          description: t(`approval.modes.${mode}.description`),
        }))}
      />
      {value === "off" ? <Alert tone="danger" title={t("approval.offWarning")} /> : null}
      {value === "http_writes" ? (
        <Alert tone="warn" title={t("approval.httpWritesWarning")} />
      ) : null}
    </div>
  );
}

export function ApprovalLimitsTab({ form }: { form: UseFormReturn<AgentForm> }) {
  const { t } = useTranslation("agents");
  const mode = useWatch({ control: form.control, name: "approval_mode" });
  const errors = form.formState.errors.sandbox;
  return (
    <div className="flex flex-col gap-6">
      <Panel title={t("approval.title")} description={t("approval.description")}>
        <ApprovalModeSelect
          value={mode}
          onChange={(next) => form.setValue("approval_mode", next, { shouldDirty: true })}
        />
      </Panel>
      <Panel title={t("limits.title")}>
        <LimitsFields form={form} prefix="limits" />
      </Panel>
      <Panel title={t("limits.sandbox")}>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label={t("limits.memory_limit_mb")}
            hint={t("limits.memory_limit_mb_hint")}
            error={errors?.memory_limit_mb?.message}
          >
            <Input
              type="number"
              step="1"
              {...form.register("sandbox.memory_limit_mb", { setValueAs: toNumberOrNull })}
            />
          </Field>
          <Field
            label={t("limits.max_processes")}
            hint={t("limits.max_processes_hint")}
            error={errors?.max_processes?.message}
          >
            <Input
              type="number"
              step="1"
              {...form.register("sandbox.max_processes", { setValueAs: toNumberOrNull })}
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}
