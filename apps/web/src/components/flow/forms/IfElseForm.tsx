import { useMemo } from "react";
import { Controller, useFieldArray, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import {
  ifElseFromForm,
  ifElseSchema,
  ifElseToForm,
  newCondition,
  type IfElseForm as IfElseFormValues,
} from "@/features/workflows/configSchemas";
import { nextItemId } from "@/features/workflows/graph";
import { selectorType, useVariableGroups, type VarGroup } from "@/features/workflows/useUpstream";
import { VariablePicker } from "../VariablePicker";
import { ConditionRow, LogicalSelect } from "./ConditionRow";
import {
  AddButton,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

function CaseCondition({
  form,
  caseIndex,
  index,
  nodeId,
  groups,
  serverIssue,
  onRemove,
}: {
  form: UseFormReturn<IfElseFormValues>;
  caseIndex: number;
  index: number;
  nodeId: string;
  groups: VarGroup[];
  serverIssue: (path: string) => string | undefined;
  onRemove: () => void;
}) {
  const { t } = useTranslation("workflows");
  const path = `cases.${caseIndex}.conditions.${index}` as const;
  const selector = useWatch({ control: form.control, name: `${path}.var` });
  return (
    <ConditionRow
      form={form}
      path={path}
      type={selectorType(groups, selector ?? "")}
      removeLabel={t("forms.ifElse.removeCondition", { n: index + 1 })}
      serverIssue={serverIssue}
      onRemove={onRemove}
      subject={
        <Controller
          control={form.control}
          name={`${path}.var`}
          render={({ field }) => (
            <VariablePicker
              nodeId={nodeId}
              value={field.value}
              onChange={field.onChange}
              aria-label={`${t("forms.ifElse.case", { n: caseIndex + 1 })} ${t("common.variable")} ${index + 1}`}
              className="min-w-0 flex-1"
            />
          )}
        />
      }
    />
  );
}

function CaseEditor({
  form,
  index,
  nodeId,
  groups,
  serverIssue,
  onRemove,
  removable,
}: {
  form: UseFormReturn<IfElseFormValues>;
  index: number;
  nodeId: string;
  groups: VarGroup[];
  serverIssue: (path: string) => string | undefined;
  onRemove: () => void;
  removable: boolean;
}) {
  const { t } = useTranslation("workflows");
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: `cases.${index}.conditions`,
  });
  const caseId = useWatch({ control: form.control, name: `cases.${index}.id` });
  return (
    <div className="flex flex-col gap-2 rounded-md border border-line p-2.5">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-ink">
          {index === 0 ? t("forms.ifElse.if") : t("forms.ifElse.elif")}
        </span>
        <Badge className="font-mono">case_{caseId}</Badge>
        <div className="ml-auto flex items-center gap-1">
          {fields.length > 1 ? <LogicalSelect form={form} name={`cases.${index}.logical`} /> : null}
          {removable ? (
            <RemoveButton
              label={t("forms.ifElse.removeCase", { n: index + 1 })}
              onClick={onRemove}
            />
          ) : null}
        </div>
      </div>
      {fields.map((field, conditionIndex) => (
        <CaseCondition
          key={field.id}
          form={form}
          caseIndex={index}
          index={conditionIndex}
          nodeId={nodeId}
          groups={groups}
          serverIssue={serverIssue}
          onRemove={() => remove(conditionIndex)}
        />
      ))}
      <FieldMessage message={errorAt(form, `cases.${index}.conditions`, serverIssue)} />
      <div>
        <AddButton label={t("forms.ifElse.addCondition")} onClick={() => append(newCondition())} />
      </div>
    </div>
  );
}

// 조건 빌더(설계 7.3 IfElseForm). 케이스 id는 핸들 이름(case_<id>)이라 자동으로 붙이고 바꾸지 않는다.
export function IfElseForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => ifElseSchema(tc, t), [tc, t]);
  const form = useNodeForm<IfElseFormValues>(node, schema, ifElseToForm, ifElseFromForm);
  const serverIssue = useServerIssue(node.id);
  const groups = useVariableGroups(node.id);
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "cases",
    keyName: "key",
  });
  const cases = useWatch({ control: form.control, name: "cases" });
  return (
    <Section
      title={t("nodes.if_else.label")}
      actions={
        <AddButton
          label={t("forms.ifElse.addCase")}
          onClick={() =>
            append({
              id: nextItemId((cases ?? []).map((item) => item.id)),
              logical: "and",
              conditions: [newCondition()],
            })
          }
        />
      }
    >
      {fields.map((field, index) => (
        <CaseEditor
          key={field.key}
          form={form}
          index={index}
          nodeId={node.id}
          groups={groups}
          serverIssue={serverIssue}
          removable={fields.length > 1}
          onRemove={() => remove(index)}
        />
      ))}
      <FieldMessage message={errorAt(form, "cases", serverIssue)} />
      <p className="rounded-md bg-sunken px-3 py-2 text-xs text-ink-muted">
        {t("forms.ifElse.else")}
      </p>
    </Section>
  );
}
