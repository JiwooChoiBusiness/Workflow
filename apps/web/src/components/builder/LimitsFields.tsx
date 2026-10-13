import { get, type FieldValues, type Path, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { toNumber, toNumberOrNull } from "@/lib/formSchemas";

const LIMIT_FIELDS = [
  { name: "max_steps", optional: false, step: "1" },
  { name: "max_run_seconds", optional: false, step: "1" },
  { name: "max_total_tokens", optional: false, step: "1" },
  { name: "tool_timeout_sec", optional: false, step: "1" },
  { name: "context_budget_tokens", optional: true, step: "1" },
  { name: "max_cost_usd", optional: true, step: "any" },
] as const;

// 에이전트 초안의 limits와 워크스페이스 기본 한도(default_limits)가 같은 구조(RunLimits)를 쓴다.
export function LimitsFields<F extends FieldValues>({
  form,
  prefix,
}: {
  form: UseFormReturn<F>;
  prefix: string;
}) {
  const { t } = useTranslation("agents");
  const errors = form.formState.errors;
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {LIMIT_FIELDS.map((field) => {
        const path = `${prefix}.${field.name}` as Path<F>;
        const error = get(errors, path) as { message?: string } | undefined;
        return (
          <Field
            key={field.name}
            label={t(`limits.${field.name}`)}
            hint={t(`limits.${field.name}_hint`)}
            error={error?.message}
          >
            <Input
              type="number"
              inputMode="decimal"
              step={field.step}
              {...form.register(path, { setValueAs: field.optional ? toNumberOrNull : toNumber })}
            />
          </Field>
        );
      })}
    </div>
  );
}
