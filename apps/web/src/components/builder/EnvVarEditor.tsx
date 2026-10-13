import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useFieldArray, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { Switch } from "@/components/ui/toggle";
import type { AgentForm } from "@/features/agents/schemas";

export function EnvVarEditor({ form }: { form: UseFormReturn<AgentForm> }) {
  const { t } = useTranslation("agents");
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "env_vars" });
  const values = useWatch({ control: form.control, name: "env_vars" });
  const errors = form.formState.errors.env_vars;

  const addButton = (
    <Button size="sm" onClick={() => append({ name: "", value: "", secret: false, keep: false })}>
      <Plus className="size-3.5" aria-hidden />
      {t("env.add")}
    </Button>
  );

  return (
    <Panel title={t("env.title")} description={t("env.description")} actions={addButton}>
      {fields.length === 0 ? (
        <EmptyState icon={KeyRound} title={t("env.empty")} />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-[minmax(0,14rem)_minmax(0,1fr)_4rem_2rem] gap-2 px-0.5 text-xs font-medium text-ink-muted">
            <span>{t("env.name")}</span>
            <span>{t("env.value")}</span>
            <span>{t("env.secret")}</span>
            <span />
          </div>
          {fields.map((field, index) => {
            const row = values?.[index];
            const secret = row?.secret ?? false;
            const keepsStored = row?.keep ?? false;
            const rowErrors = errors?.[index];
            const label = row?.name || t("env.unnamed");
            return (
              <div key={field.id} className="flex flex-col gap-1">
                <div className="grid grid-cols-[minmax(0,14rem)_minmax(0,1fr)_4rem_2rem] items-center gap-2">
                  <Input
                    className="font-mono"
                    aria-label={`${t("env.name")} ${index + 1}`}
                    aria-invalid={rowErrors?.name ? true : undefined}
                    {...form.register(`env_vars.${index}.name`)}
                  />
                  <Input
                    className="font-mono"
                    type={secret ? "password" : "text"}
                    autoComplete="off"
                    aria-invalid={rowErrors?.value ? true : undefined}
                    aria-label={`${t("env.value")} ${index + 1}`}
                    placeholder={keepsStored ? t("env.keepSecret") : undefined}
                    {...form.register(`env_vars.${index}.value`)}
                  />
                  <Switch
                    aria-label={`${t("env.secret")} ${index + 1}`}
                    checked={secret}
                    onCheckedChange={(on) => {
                      form.setValue(`env_vars.${index}.secret`, on, { shouldDirty: true });
                      // 비밀 값을 일반 값으로 바꾸면 서버가 값을 다시 받는다(env_value_required).
                      if (!on) form.setValue(`env_vars.${index}.keep`, false);
                    }}
                  />
                  <Button
                    variant="dangerGhost"
                    size="iconSm"
                    aria-label={t("env.remove", { name: label })}
                    onClick={() => remove(index)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </div>
                {rowErrors?.name?.message || rowErrors?.value?.message ? (
                  <p className="text-xs text-danger" role="alert">
                    {rowErrors.name?.message ?? rowErrors.value?.message}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
