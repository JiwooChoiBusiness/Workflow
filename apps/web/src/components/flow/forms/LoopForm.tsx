import { useMemo } from "react";
import { Controller, useFieldArray, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  loopFromForm,
  loopSchema,
  loopToForm,
  newCondition,
  newLoopVar,
  VAR_TYPE_OPTIONS,
  type LoopForm as LoopFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { LOOP_MAX_ITERATIONS } from "@/features/workflows/nodeTypes";
import { selectorType, useVariableGroups, type VarGroup } from "@/features/workflows/useUpstream";
import { toNumber } from "@/lib/formSchemas";
import { VariablePicker } from "../VariablePicker";
import { ConditionRow, LogicalSelect } from "./ConditionRow";
import {
  AddButton,
  EmptyRows,
  errorAt,
  FieldMessage,
  RemoveButton,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

function LoopVarRows({
  form,
  nodeId,
  serverIssue,
}: {
  form: UseFormReturn<LoopFormValues>;
  nodeId: string;
  serverIssue: (path: string) => string | undefined;
}) {
  const { t } = useTranslation("workflows");
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "loop_vars" });
  const rows = useWatch({ control: form.control, name: "loop_vars" });
  const example = rows?.find((row) => row.name.trim())?.name.trim() ?? "count";
  const typeOptions = VAR_TYPE_OPTIONS.map((type) => ({ value: type, label: type }));
  const sourceOptions = [
    { value: "value", label: t("forms.loop.fromValue") },
    { value: "var", label: t("forms.loop.fromVar") },
  ];
  return (
    <Section
      title={t("forms.loop.vars")}
      hint={t("forms.loop.varsHint", { example: `{{ ${nodeId}.${example} }}` })}
      actions={<AddButton label={t("forms.loop.addVar")} onClick={() => append(newLoopVar())} />}
    >
      {fields.length === 0 ? <EmptyRows>{t("forms.loop.noVars")}</EmptyRows> : null}
      {fields.map((field, index) => {
        const row = rows?.[index];
        const path = `loop_vars.${index}` as const;
        const label = row?.name || t("common.row", { n: index + 1 });
        const errors = [
          errorAt(form, `${path}.var`, serverIssue),
          errorAt(form, `${path}.value_text`, serverIssue) ?? serverIssue(`${path}.value`),
          serverIssue(path),
        ].filter(Boolean);
        return (
          <div key={field.id} className="flex flex-col gap-2 rounded-md border border-line p-2.5">
            <div className="flex items-start gap-1.5">
              <Field
                className="min-w-0 flex-1"
                label={t("forms.loop.varName")}
                error={errorAt(form, `${path}.name`, serverIssue)}
                required
              >
                <Input className="font-mono" {...form.register(`${path}.name`)} />
              </Field>
              <Field className="w-36 shrink-0" label={t("common.type")}>
                <Controller
                  control={form.control}
                  name={`${path}.type`}
                  render={({ field: control }) => (
                    <Select
                      value={control.value}
                      onValueChange={control.onChange}
                      options={typeOptions}
                    />
                  )}
                />
              </Field>
              <div className="pt-6">
                <RemoveButton
                  label={t("common.remove", { name: label })}
                  onClick={() => remove(index)}
                />
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <Controller
                control={form.control}
                name={`${path}.source`}
                render={({ field: control }) => (
                  <Select
                    aria-label={`${label} ${t("forms.loop.initial")}`}
                    className="w-24 shrink-0"
                    value={control.value}
                    onValueChange={control.onChange}
                    options={sourceOptions}
                  />
                )}
              />
              {row?.source === "var" ? (
                <Controller
                  control={form.control}
                  name={`${path}.var`}
                  render={({ field: control }) => (
                    <VariablePicker
                      nodeId={nodeId}
                      value={control.value}
                      onChange={control.onChange}
                      aria-label={`${label} ${t("forms.loop.fromVar")}`}
                      className="min-w-0 flex-1"
                    />
                  )}
                />
              ) : (
                <Input
                  className="min-w-0 flex-1 font-mono"
                  aria-label={`${label} ${t("forms.loop.fromValue")}`}
                  placeholder={t("forms.loop.valuePlaceholder")}
                  {...form.register(`${path}.value_text`)}
                />
              )}
            </div>
            {errors.map((message) => (
              <FieldMessage key={message} message={message} />
            ))}
          </div>
        );
      })}
      <FieldMessage message={errorAt(form, "loop_vars", serverIssue)} />
    </Section>
  );
}

function BreakCondition({
  form,
  index,
  nodeId,
  groups,
  serverIssue,
  onRemove,
}: {
  form: UseFormReturn<LoopFormValues>;
  index: number;
  nodeId: string;
  groups: VarGroup[];
  serverIssue: (path: string) => string | undefined;
  onRemove: () => void;
}) {
  const { t } = useTranslation("workflows");
  const path = `break_conditions.${index}` as const;
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
              scope="loop"
              value={field.value}
              onChange={field.onChange}
              aria-label={t("forms.loop.conditionVar", { n: index + 1 })}
              className="min-w-0 flex-1"
            />
          )}
        />
      }
    />
  );
}

/*
 * loop(설계 2.4, 서버 LoopConfig): 반복 변수 표, 종료 조건(if_else 조건 빌더와 같은 줄), 최대 반복 수. 안쪽
 * 노드는 캔버스에서 루프 틀 안에 놓고, 반복 변수는 그 안의 변수 쓰기 노드가 바꾼다.
 */
export function LoopForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => loopSchema(tc, t), [tc, t]);
  const form = useNodeForm<LoopFormValues>(node, schema, loopToForm, loopFromForm);
  const serverIssue = useServerIssue(node.id);
  const groups = useVariableGroups(node.id, "loop");
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "break_conditions",
  });
  const children = useFlow(
    (state) => state.graph.nodes.filter((item) => item.parent_id === node.id).length,
  );
  return (
    <div className="flex flex-col gap-4">
      <LoopVarRows form={form} nodeId={node.id} serverIssue={serverIssue} />
      <Section
        title={t("forms.loop.break")}
        hint={t("forms.loop.breakHint")}
        actions={
          <>
            {fields.length > 1 ? <LogicalSelect form={form} name="logical" /> : null}
            <AddButton
              label={t("forms.ifElse.addCondition")}
              onClick={() => append(newCondition())}
            />
          </>
        }
      >
        {fields.length === 0 ? <EmptyRows>{t("forms.loop.noBreak")}</EmptyRows> : null}
        {fields.map((field, index) => (
          <BreakCondition
            key={field.id}
            form={form}
            index={index}
            nodeId={node.id}
            groups={groups}
            serverIssue={serverIssue}
            onRemove={() => remove(index)}
          />
        ))}
        <FieldMessage message={errorAt(form, "break_conditions", serverIssue)} />
      </Section>
      <Field
        label={t("forms.loop.max")}
        hint={t("forms.loop.maxHint", { max: LOOP_MAX_ITERATIONS })}
        error={errorAt(form, "max_iterations", serverIssue)}
      >
        <Input
          type="number"
          step="1"
          min={1}
          max={LOOP_MAX_ITERATIONS}
          className="w-24"
          {...form.register("max_iterations", { setValueAs: toNumber })}
        />
      </Field>
      <p className="rounded-md bg-sunken px-3 py-2 text-xs text-ink-muted">
        {t("forms.loop.children", { count: children })}
      </p>
    </div>
  );
}
