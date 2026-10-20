import type { ReactNode } from "react";
import {
  Controller,
  useWatch,
  type FieldValues,
  type Path,
  type UseFormReturn,
} from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { VarType } from "@/api/types";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  LIST_OPS,
  OP_KEYS,
  opsFor,
  UNARY_OPS,
  type IfElseOp,
} from "@/features/workflows/configSchemas";
import { errorAt, FieldMessage, RemoveButton } from "./shared";

/*
 * 조건 하나(비교할 값, 연산자, 비교 값). if_else 케이스·loop 종료 조건·list_operator 필터가 같이 쓴다. 조건의
 * 폼 값은 {<subjectField>, op, value_text}이고 path가 그 자리다("cases.0.conditions.1"). 연산자는 비교할 값의
 * 타입에 맞는 것만 보인다(서버 검증과 같은 표).
 */
export function ConditionRow<F extends FieldValues>({
  form,
  path,
  subject,
  subjectField = "var",
  type,
  removeLabel,
  serverIssue,
  onRemove,
}: {
  form: UseFormReturn<F>;
  path: string;
  subject: ReactNode;
  subjectField?: "var" | "key";
  type: VarType | null;
  removeLabel: string;
  serverIssue: (path: string) => string | undefined;
  onRemove: () => void;
}) {
  const { t } = useTranslation("workflows");
  const op = useWatch({ control: form.control, name: `${path}.op` as Path<F> }) as IfElseOp;
  const allowed = opsFor(type);
  const ops = allowed.includes(op) ? allowed : [...allowed, op];
  const errors = [
    errorAt(form, `${path}.${subjectField}`, serverIssue),
    errorAt(form, `${path}.op`, serverIssue),
    errorAt(form, `${path}.value_text`, serverIssue) ?? serverIssue(`${path}.value`),
  ].filter(Boolean);
  return (
    <div className="flex flex-col gap-1 rounded-md bg-sunken p-2">
      <div className="flex items-center gap-1.5">
        {subject}
        <RemoveButton label={removeLabel} onClick={onRemove} />
      </div>
      <div className="flex items-center gap-1.5">
        <Controller
          control={form.control}
          name={`${path}.op` as Path<F>}
          render={({ field }) => (
            <Select
              aria-label={t("forms.ifElse.op")}
              className="w-36 shrink-0"
              value={field.value as string}
              onValueChange={field.onChange}
              options={ops.map((item) => ({
                value: item,
                label: `${item} · ${t(`forms.ifElse.ops.${OP_KEYS[item]}`)}`,
              }))}
            />
          )}
        />
        {UNARY_OPS.has(op) ? null : type === "boolean" && !LIST_OPS.has(op) ? (
          <Controller
            control={form.control}
            name={`${path}.value_text` as Path<F>}
            render={({ field }) => (
              <Select
                aria-label={t("forms.ifElse.value")}
                className="min-w-0 flex-1"
                value={(field.value as string) || "true"}
                onValueChange={field.onChange}
                options={[
                  { value: "true", label: "true" },
                  { value: "false", label: "false" },
                ]}
              />
            )}
          />
        ) : (
          <Input
            className="min-w-0 flex-1"
            aria-label={t("forms.ifElse.value")}
            placeholder={LIST_OPS.has(op) ? t("forms.ifElse.listHint") : t("forms.ifElse.value")}
            {...form.register(`${path}.value_text` as Path<F>)}
          />
        )}
      </div>
      {errors.map((message) => (
        <FieldMessage key={message} message={message} />
      ))}
    </div>
  );
}

// 조건 묶기(and/or). 조건이 둘 이상일 때만 보인다.
export function LogicalSelect<F extends FieldValues>({
  form,
  name,
  className,
}: {
  form: UseFormReturn<F>;
  name: string;
  className?: string;
}) {
  const { t } = useTranslation("workflows");
  return (
    <Controller
      control={form.control}
      name={name as Path<F>}
      render={({ field }) => (
        <Select
          aria-label={t("forms.ifElse.logical")}
          className={className ?? "h-7 w-32 text-xs"}
          value={field.value as string}
          onValueChange={field.onChange}
          options={[
            { value: "and", label: t("forms.ifElse.and") },
            { value: "or", label: t("forms.ifElse.or") },
          ]}
        />
      )}
    />
  );
}
