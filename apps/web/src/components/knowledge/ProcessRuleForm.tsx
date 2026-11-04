import { Plus, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { DocForm } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import {
  escapeSeparator,
  MAX_SEPARATORS,
  unescapeSeparator,
  type RuleError,
  type RuleField,
  type RuleValues,
} from "@/features/knowledge/processRule";

function SeparatorEditor({
  label,
  values,
  onChange,
  error,
  disabled,
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  error?: string;
  disabled?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = unescapeSeparator(draft);
    if (!value || values.includes(value) || values.length >= MAX_SEPARATORS) return;
    onChange([...values, value]);
    setDraft("");
  };
  return (
    <Field label={label} hint={t("rule.separatorsHint")} error={error}>
      <div className="flex flex-wrap items-center gap-1.5">
        {values.map((value) => (
          <span
            key={value}
            className="inline-flex h-6 items-center gap-1 rounded-sm border border-line bg-sunken pl-1.5 font-mono text-xs text-ink"
          >
            {escapeSeparator(value)}
            <button
              type="button"
              className="grid size-5 place-items-center rounded-sm text-ink-subtle hover:text-ink disabled:opacity-50"
              aria-label={t("rule.removeSeparator", { value: escapeSeparator(value) })}
              disabled={disabled || values.length <= 1}
              onClick={() => onChange(values.filter((item) => item !== value))}
            >
              <X className="size-3" aria-hidden />
            </button>
          </span>
        ))}
        <Input
          value={draft}
          disabled={disabled}
          aria-label={t("rule.addSeparator")}
          placeholder="\n"
          className="h-6 w-20 font-mono text-xs"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button
          size="iconSm"
          variant="ghost"
          disabled={disabled || !draft}
          aria-label={t("rule.addSeparator")}
          title={t("rule.addSeparator")}
          onClick={add}
        >
          <Plus className="size-3.5" aria-hidden />
        </Button>
      </div>
    </Field>
  );
}

function NumberField({
  label,
  value,
  onChange,
  error,
  disabled,
  min,
  max,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  error?: string;
  disabled?: boolean;
  min: number;
  max: number;
}) {
  return (
    <Field label={label} error={error}>
      <Input
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={Number.isNaN(value) ? "" : value}
        disabled={disabled}
        onChange={(event) =>
          onChange(event.target.value === "" ? Number.NaN : Number(event.target.value))
        }
      />
    </Field>
  );
}

// 정리·청크 규칙(설계 6.3). 청크 모드에 맞는 칸만 보이고, Q&A는 나누지 않아 정리 규칙뿐이다.
export function ProcessRuleForm({
  value,
  onChange,
  docForm,
  errors = {},
  disabled,
}: {
  value: RuleValues;
  onChange: (next: RuleValues) => void;
  docForm: DocForm;
  errors?: Partial<Record<RuleField, RuleError>>;
  disabled?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const message = (field: RuleField) => {
    const code = errors[field];
    return code ? t(`rule.errors.${code}`) : undefined;
  };
  const set = <K extends keyof RuleValues>(key: K, patch: Partial<RuleValues[K]>) =>
    onChange({ ...value, [key]: { ...value[key], ...patch } });

  return (
    <div className="flex flex-col gap-4">
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium text-ink">{t("rule.cleaning")}</legend>
        <Field label={t("rule.removeExtraSpaces")} inline>
          <Switch
            checked={value.cleaning.remove_extra_spaces}
            disabled={disabled}
            onCheckedChange={(checked) => set("cleaning", { remove_extra_spaces: checked })}
          />
        </Field>
        <Field label={t("rule.removeUrlsEmails")} inline>
          <Switch
            checked={value.cleaning.remove_urls_emails}
            disabled={disabled}
            onCheckedChange={(checked) => set("cleaning", { remove_urls_emails: checked })}
          />
        </Field>
      </fieldset>
      {docForm === "text_model" ? (
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-sm font-medium text-ink">{t("rule.general")}</legend>
          <SeparatorEditor
            label={t("rule.separators")}
            values={value.general.separators}
            disabled={disabled}
            error={message("general.separators")}
            onChange={(separators) => set("general", { separators })}
          />
          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label={t("rule.maxTokens")}
              value={value.general.max_tokens}
              min={20}
              max={4000}
              disabled={disabled}
              error={message("general.max_tokens")}
              onChange={(max_tokens) => set("general", { max_tokens })}
            />
            <NumberField
              label={t("rule.overlapTokens")}
              value={value.general.overlap_tokens}
              min={0}
              max={3999}
              disabled={disabled}
              error={message("general.overlap_tokens")}
              onChange={(overlap_tokens) => set("general", { overlap_tokens })}
            />
          </div>
        </fieldset>
      ) : null}
      {docForm === "hierarchical_model" ? (
        <>
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-1 text-sm font-medium text-ink">{t("rule.parent")}</legend>
            <Field label={t("rule.parentMode")}>
              <Select
                value={value.parent.mode}
                disabled={disabled}
                onValueChange={(mode) =>
                  set("parent", { mode: mode as RuleValues["parent"]["mode"] })
                }
                options={[
                  { value: "paragraph", label: t("rule.paragraph") },
                  { value: "full_doc", label: t("rule.fullDoc") },
                ]}
              />
            </Field>
            <SeparatorEditor
              label={t("rule.separators")}
              values={value.parent.separators}
              disabled={disabled}
              error={message("parent.separators")}
              onChange={(separators) => set("parent", { separators })}
            />
            {value.parent.mode === "paragraph" ? (
              <NumberField
                label={t("rule.maxTokens")}
                value={value.parent.max_tokens}
                min={20}
                max={4000}
                disabled={disabled}
                error={message("parent.max_tokens")}
                onChange={(max_tokens) => set("parent", { max_tokens })}
              />
            ) : null}
          </fieldset>
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-1 text-sm font-medium text-ink">{t("rule.child")}</legend>
            <SeparatorEditor
              label={t("rule.separators")}
              values={value.child.separators}
              disabled={disabled}
              error={message("child.separators")}
              onChange={(separators) => set("child", { separators })}
            />
            <NumberField
              label={t("rule.maxTokens")}
              value={value.child.max_tokens}
              min={20}
              max={4000}
              disabled={disabled}
              error={message("child.max_tokens")}
              onChange={(max_tokens) => set("child", { max_tokens })}
            />
          </fieldset>
        </>
      ) : null}
    </div>
  );
}
