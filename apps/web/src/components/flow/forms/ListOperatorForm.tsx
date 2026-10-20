import { useMemo, type ReactNode } from "react";
import { Controller, useFieldArray, useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { VarType } from "@/api/types";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import {
  FILE_ITEM_KEYS,
  LIST_SORT_ORDERS,
  listOperatorFromForm,
  listOperatorSchema,
  listOperatorToForm,
  newItemCondition,
  type ListOperatorForm as ListOperatorFormValues,
} from "@/features/workflows/configSchemas";
import { selectorType, useVariableGroups } from "@/features/workflows/useUpstream";
import { toNumber, toNumberOrNull } from "@/lib/formSchemas";
import { VariablePicker } from "../VariablePicker";
import { ConditionRow, LogicalSelect } from "./ConditionRow";
import {
  AddButton,
  errorAt,
  FieldMessage,
  Section,
  useNodeForm,
  useServerIssue,
  type NodeFormProps,
} from "./shared";

type Form = UseFormReturn<ListOperatorFormValues>;
type Toggle = "filter_enabled" | "sort_enabled" | "slice_enabled" | "extract_enabled";

const ARRAY_TYPES = [
  "array[string]",
  "array[number]",
  "array[boolean]",
  "array[object]",
  "array[file]",
  "array[any]",
] as const;

const FILE_KEYS = Object.keys(FILE_ITEM_KEYS) as (keyof typeof FILE_ITEM_KEYS)[];
const PICK = "__pick__";

// 배열 원소의 타입. 모르면 null이다.
function itemType(type: VarType | null): VarType | null {
  const match = type ? /^array\[(.+)\]$/.exec(type) : null;
  return match ? (match[1] as VarType) : null;
}

// 원소에서 key로 꺼낸 값의 타입. 연산자 목록을 정한다.
function keyType(item: VarType | null, key: string): VarType | null {
  if (!key.trim()) return item;
  if (item === "file") return FILE_ITEM_KEYS[key as keyof typeof FILE_ITEM_KEYS] ?? null;
  return null;
}

function ToggleSection({
  form,
  name,
  title,
  hint,
  children,
}: {
  form: Form;
  name: Toggle;
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  const on = useWatch({ control: form.control, name });
  return (
    <Section
      title={title}
      hint={on ? hint : undefined}
      actions={
        <Controller
          control={form.control}
          name={name}
          render={({ field }) => (
            <Switch aria-label={title} checked={field.value} onCheckedChange={field.onChange} />
          )}
        />
      }
    >
      {on ? children : null}
    </Section>
  );
}

// 원소에서 비교·정렬할 값. 파일 배열은 name·size·mime·extension 중에서 고르고, 그 밖은 비우거나 키를 쓴다.
function KeyInput({
  form,
  name,
  files,
  label,
}: {
  form: Form;
  name: "sort.key" | `filter.conditions.${number}.key`;
  files: boolean;
  label: string;
}) {
  const { t } = useTranslation("workflows");
  if (files) {
    return (
      <Controller
        control={form.control}
        name={name}
        render={({ field }) => (
          <Select
            aria-label={label}
            className="min-w-0 flex-1"
            value={field.value || PICK}
            onValueChange={field.onChange}
            options={[
              { value: PICK, label: t("forms.listOperator.pickKey"), disabled: true },
              ...FILE_KEYS.map((key) => ({
                value: key,
                label: t(`forms.listOperator.fileKeys.${key}`),
                hint: key,
              })),
            ]}
          />
        )}
      />
    );
  }
  return (
    <Input
      aria-label={label}
      className="min-w-0 flex-1 font-mono"
      placeholder={t("forms.listOperator.keyPlaceholder")}
      {...form.register(name)}
    />
  );
}

function FilterRows({
  form,
  files,
  item,
  serverIssue,
}: {
  form: Form;
  files: boolean;
  item: VarType | null;
  serverIssue: (path: string) => string | undefined;
}) {
  const { t } = useTranslation("workflows");
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "filter.conditions",
  });
  const conditions = useWatch({ control: form.control, name: "filter.conditions" });
  return (
    <>
      {fields.map((field, index) => (
        <ConditionRow
          key={field.id}
          form={form}
          path={`filter.conditions.${index}`}
          subjectField="key"
          type={keyType(item, conditions?.[index]?.key ?? "")}
          removeLabel={t("forms.ifElse.removeCondition", { n: index + 1 })}
          serverIssue={serverIssue}
          onRemove={() => remove(index)}
          subject={
            <KeyInput
              form={form}
              name={`filter.conditions.${index}.key`}
              files={files}
              label={t("forms.listOperator.conditionKey", { n: index + 1 })}
            />
          }
        />
      ))}
      <FieldMessage message={errorAt(form, "filter.conditions", serverIssue)} />
      <div className="flex items-center gap-1.5">
        <AddButton
          label={t("forms.ifElse.addCondition")}
          onClick={() => append(newItemCondition())}
        />
        {fields.length > 1 ? (
          <LogicalSelect form={form} name="filter.logical" className="ml-auto h-7 w-32 text-xs" />
        ) : null}
      </div>
    </>
  );
}

/*
 * list_operator(설계 2.4, 서버 ListOperatorConfig): 입력 배열, 거르기(if_else와 같은 연산자), 정렬, 자르기,
 * 하나 꺼내기. 차례는 거르기 → 정렬 → 자르기이고 꺼내기는 그 결과에서 고른다.
 */
export function ListOperatorForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => listOperatorSchema(tc, t), [tc, t]);
  const form = useNodeForm<ListOperatorFormValues>(
    node,
    schema,
    listOperatorToForm,
    listOperatorFromForm,
  );
  const serverIssue = useServerIssue(node.id);
  const groups = useVariableGroups(node.id);
  const input = useWatch({ control: form.control, name: "input_var" });
  const item = itemType(selectorType(groups, input ?? ""));
  const files = item === "file";
  return (
    <div className="flex flex-col gap-4">
      <Field
        label={t("forms.listOperator.input")}
        hint={t("forms.listOperator.inputHint")}
        error={errorAt(form, "input_var", serverIssue)}
      >
        <Controller
          control={form.control}
          name="input_var"
          render={({ field }) => (
            <VariablePicker
              nodeId={node.id}
              value={field.value}
              onChange={field.onChange}
              accept={ARRAY_TYPES}
            />
          )}
        />
      </Field>
      <ToggleSection
        form={form}
        name="filter_enabled"
        title={t("forms.listOperator.filter")}
        hint={files ? t("forms.listOperator.fileKeyHint") : t("forms.listOperator.keyHint")}
      >
        <FilterRows form={form} files={files} item={item} serverIssue={serverIssue} />
      </ToggleSection>
      <ToggleSection form={form} name="sort_enabled" title={t("forms.listOperator.sort")}>
        <div className="flex items-center gap-1.5">
          <KeyInput
            form={form}
            name="sort.key"
            files={files}
            label={t("forms.listOperator.sortKey")}
          />
          <Controller
            control={form.control}
            name="sort.order"
            render={({ field }) => (
              <Select
                aria-label={t("forms.listOperator.order")}
                className="w-32 shrink-0"
                value={field.value}
                onValueChange={field.onChange}
                options={LIST_SORT_ORDERS.map((order) => ({
                  value: order,
                  label: t(`forms.listOperator.orders.${order}`),
                }))}
              />
            )}
          />
        </div>
        <FieldMessage message={errorAt(form, "sort.key", serverIssue)} />
      </ToggleSection>
      <ToggleSection
        form={form}
        name="slice_enabled"
        title={t("forms.listOperator.slice")}
        hint={t("forms.listOperator.sliceHint")}
      >
        <div className="grid grid-cols-2 gap-2">
          <Field
            label={t("forms.listOperator.offset")}
            error={errorAt(form, "slice.offset", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              min={0}
              {...form.register("slice.offset", { setValueAs: toNumber })}
            />
          </Field>
          <Field
            label={t("forms.listOperator.limit")}
            error={errorAt(form, "slice.limit", serverIssue)}
          >
            <Input
              type="number"
              step="1"
              min={0}
              placeholder={t("forms.listOperator.limitPlaceholder")}
              {...form.register("slice.limit", { setValueAs: toNumberOrNull })}
            />
          </Field>
        </div>
      </ToggleSection>
      <ToggleSection
        form={form}
        name="extract_enabled"
        title={t("forms.listOperator.extract")}
        hint={t("forms.listOperator.extractHint")}
      >
        <Field
          label={t("forms.listOperator.index")}
          error={errorAt(form, "extract_index", serverIssue)}
        >
          <Input
            type="number"
            step="1"
            className="w-24"
            {...form.register("extract_index", { setValueAs: toNumber })}
          />
        </Field>
      </ToggleSection>
    </div>
  );
}
