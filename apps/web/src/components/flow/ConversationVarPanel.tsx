import { zodResolver } from "@hookform/resolvers/zod";
import { Variable } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Controller, useFieldArray, useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import * as z from "zod";
import type { ConversationVar } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import {
  literalText,
  literalValue,
  NAME_PATTERN,
  VAR_TYPE_OPTIONS,
} from "@/features/workflows/configSchemas";
import { useFlowActions } from "@/features/workflows/flowStoreContext";
import type { T } from "@/lib/formSchemas";
import { AddButton, FieldMessage, RemoveButton } from "./forms/shared";

function convSchema(t: T, nameMessage: string) {
  return z.object({
    vars: z
      .array(
        z.object({
          name: z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .regex(NAME_PATTERN, { error: nameMessage }),
          type: z.enum(VAR_TYPE_OPTIONS),
          default_text: z.string(),
        }),
      )
      .max(100, { error: t("validation.maxItems", { max: 100 }) })
      .superRefine((rows, ctx) => {
        const seen = new Set<string>();
        rows.forEach((row, index) => {
          const name = row.name.trim();
          if (name && seen.has(name)) {
            ctx.addIssue({
              code: "custom",
              path: [index, "name"],
              message: t("validation.duplicate"),
            });
          }
          seen.add(name);
        });
      }),
  });
}

type ConvForm = z.infer<ReturnType<typeof convSchema>>;

function toRows(vars: ConversationVar[]): ConvForm["vars"] {
  return vars.map((item) => ({
    name: item.name,
    type: item.type,
    default_text: literalText(item.default),
  }));
}

function fromRows(rows: ConvForm["vars"]): ConversationVar[] {
  return rows.map((row) => ({
    name: row.name.trim(),
    type: row.type,
    default: row.default_text.trim() === "" ? null : literalValue(row.default_text, row.type),
  }));
}

// 챗플로우 대화 변수(설계 4.1 conversation_vars). 폼 전체가 맞을 때만 초안에 넣는다(EnvVarPanel과 같음).
export function ConversationVarPanel({ initial }: { initial: ConversationVar[] }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const actions = useFlowActions();
  const schema = useMemo(() => convSchema(tc, t("validation.name")), [tc, t]);
  const form = useForm<ConvForm>({
    defaultValues: { vars: toRows(initial) },
    resolver: zodResolver(schema),
    mode: "onChange",
  });
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "vars" });
  const [blocked, setBlocked] = useState(false);
  const errors = form.formState.errors.vars;

  useEffect(() => {
    return form.subscribe({
      formState: { values: true },
      callback: (info) => {
        if (info.name === undefined) return;
        const parsed = schema.safeParse(form.getValues());
        setBlocked(!parsed.success);
        if (parsed.success) actions.setConversationVars(fromRows(parsed.data.vars));
      },
    });
  }, [form, schema, actions]);

  return (
    <Panel
      title={t("conv.title")}
      description={t("conv.description", { example: "{{ conv.이름 }}" })}
      actions={
        <AddButton
          label={t("conv.add")}
          onClick={() => append({ name: "", type: "string", default_text: "" })}
        />
      }
    >
      <div className="flex flex-col gap-2">
        {blocked ? <Alert tone="warn" title={t("conv.invalid")} /> : null}
        {fields.length === 0 ? <EmptyState icon={Variable} title={t("conv.empty")} /> : null}
        {fields.map((field, index) => {
          const row = t("common.row", { n: index + 1 });
          const rowErrors = errors?.[index];
          return (
            <div key={field.id} className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5">
                <Input
                  className="w-32 shrink-0 font-mono"
                  placeholder={t("common.name")}
                  aria-label={`${row} ${t("common.name")}`}
                  aria-invalid={rowErrors?.name ? true : undefined}
                  {...form.register(`vars.${index}.name`)}
                />
                <Controller
                  control={form.control}
                  name={`vars.${index}.type`}
                  render={({ field: control }) => (
                    <Select
                      aria-label={`${row} ${t("common.type")}`}
                      className="w-32 shrink-0"
                      value={control.value}
                      onValueChange={control.onChange}
                      options={VAR_TYPE_OPTIONS.map((type) => ({ value: type, label: type }))}
                    />
                  )}
                />
                <Input
                  className="min-w-0 flex-1 font-mono"
                  placeholder={t("conv.default")}
                  title={t("conv.defaultHint")}
                  aria-label={`${row} ${t("conv.default")}`}
                  {...form.register(`vars.${index}.default_text`)}
                />
                <RemoveButton
                  label={t("common.remove", { name: row })}
                  onClick={() => remove(index)}
                />
              </div>
              <FieldMessage message={rowErrors?.name?.message} />
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
