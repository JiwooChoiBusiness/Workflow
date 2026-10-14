import { useMemo } from "react";
import { Controller, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/toggle";
import {
  ITERATION_ERROR_MODES,
  iterationFromForm,
  iterationSchema,
  iterationToForm,
  type IterationForm as IterationFormValues,
} from "@/features/workflows/configSchemas";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { toNumber } from "@/lib/formSchemas";
import { VariablePicker } from "../VariablePicker";
import { errorAt, useNodeForm, useServerIssue, type NodeFormProps } from "./shared";

// 선택기에서 배열이 아닌 변수를 흐리게 보인다(서버는 단일 값을 1원소 배열로 바꿔 받는다).
const ARRAY_TYPES = [
  "array[string]",
  "array[number]",
  "array[boolean]",
  "array[object]",
  "array[file]",
] as const;

// 설계 2.3 iteration(서버 IterationConfig). 안쪽 노드는 캔버스에서 반복 노드 틀 안에 끌어다 놓는다.
export function IterationForm({ node }: NodeFormProps) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => iterationSchema(tc, t), [tc, t]);
  const form = useNodeForm<IterationFormValues>(node, schema, iterationToForm, iterationFromForm);
  const serverIssue = useServerIssue(node.id);
  const parallel = useWatch({ control: form.control, name: "parallel" });
  const children = useFlow(
    (state) => state.graph.nodes.filter((item) => item.parent_id === node.id).length,
  );
  return (
    <div className="flex flex-col gap-4">
      <Field label={t("forms.iteration.input")} error={errorAt(form, "input_var", serverIssue)}>
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
      <Field
        label={t("forms.iteration.output")}
        hint={`${t("forms.iteration.outputHint")} ${t("forms.iteration.children", { count: children })}`}
        error={errorAt(form, "output_var", serverIssue)}
      >
        <Controller
          control={form.control}
          name="output_var"
          render={({ field }) => (
            <VariablePicker
              nodeId={node.id}
              scope="children"
              value={field.value}
              onChange={field.onChange}
            />
          )}
        />
      </Field>
      <Field inline label={t("forms.iteration.parallel")} hint={t("forms.iteration.parallelHint")}>
        <Controller
          control={form.control}
          name="parallel"
          render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />}
        />
      </Field>
      {parallel ? (
        <Field
          label={t("forms.iteration.maxParallel")}
          error={errorAt(form, "max_parallel", serverIssue)}
        >
          <Input
            type="number"
            step="1"
            min={1}
            max={4}
            className="w-24"
            {...form.register("max_parallel", { setValueAs: toNumber })}
          />
        </Field>
      ) : null}
      <Field label={t("forms.iteration.errorMode")}>
        <Controller
          control={form.control}
          name="error_mode"
          render={({ field }) => (
            <Select
              value={field.value}
              onValueChange={field.onChange}
              options={ITERATION_ERROR_MODES.map((mode) => ({
                value: mode,
                label: t(`forms.iteration.errorModes.${mode}`),
              }))}
            />
          )}
        />
      </Field>
    </div>
  );
}
