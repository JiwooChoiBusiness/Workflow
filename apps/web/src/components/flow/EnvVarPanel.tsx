import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import * as z from "zod";
import { EnvVarEditor, type EnvVarFields } from "@/components/builder/EnvVarEditor";
import { Alert } from "@/components/ui/alert";
import { useFlowActions } from "@/features/workflows/flowStoreContext";
import type { T } from "@/lib/formSchemas";

// 서버 EnvVarIn(apps/api/app/schemas/agent_version.py)과 같은 규칙. 워크플로우 초안은 이름이 정확히 같을 때만 겹친다고 본다.
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const MAX_ENV_VALUE_CHARS = 32 * 1024;

function envSchema(t: T) {
  return z.object({
    env_vars: z
      .array(
        z.object({
          name: z
            .string()
            .trim()
            .min(1, { error: t("validation.required") })
            .regex(ENV_NAME, { error: t("validation.envName") }),
          value: z.string().max(MAX_ENV_VALUE_CHARS, {
            error: t("validation.maxLength", { max: MAX_ENV_VALUE_CHARS }),
          }),
          secret: z.boolean(),
          keep: z.boolean(),
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

// 환경 변수(설계 7.3 EnvVarPanel). 1단계 EnvVarEditor를 그대로 쓴다. 저장 요청이 422가 되지 않게 폼 전체가
// 맞을 때만 초안에 넣고, 고칠 값이 있으면 그동안은 마지막으로 맞았던 목록이 저장된다.
export function EnvVarPanel({ initial }: { initial: EnvVarFields["env_vars"] }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const actions = useFlowActions();
  const schema = useMemo(() => envSchema(tc), [tc]);
  const form = useForm<EnvVarFields>({
    defaultValues: { env_vars: initial },
    resolver: zodResolver(schema),
    mode: "onChange",
  });
  const [blocked, setBlocked] = useState(false);

  useEffect(() => {
    return form.subscribe({
      formState: { values: true },
      callback: (info) => {
        if (info.name === undefined) return;
        const parsed = schema.safeParse(form.getValues());
        setBlocked(!parsed.success);
        if (parsed.success) actions.setEnvVars(parsed.data.env_vars);
      },
    });
  }, [form, schema, actions]);

  return (
    <div className="flex flex-col gap-2">
      {blocked ? <Alert tone="warn" title={t("env.invalid")} /> : null}
      <EnvVarEditor form={form} description={t("env.description", { example: "{{ env.이름 }}" })} />
    </div>
  );
}
