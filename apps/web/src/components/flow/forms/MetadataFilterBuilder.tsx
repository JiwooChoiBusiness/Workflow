import { Plus, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, type SelectOption } from "@/components/ui/select";
import "@/features/knowledge/i18n";
import {
  conditionProblems,
  conditionType,
  FILTER_MODES,
  LIST_OPERATORS,
  MAX_FILTER_CONDITIONS,
  newCondition,
  NO_VALUE_OPERATORS,
  type FilterConditionForm,
  type FilterField,
  type FilterMode,
  type MetadataFilterForm,
} from "@/features/knowledge/metadataFilter";

type OperatorKey =
  | "is"
  | "isNot"
  | "contains"
  | "notContains"
  | "startWith"
  | "endWith"
  | "empty"
  | "notEmpty"
  | "in"
  | "notIn"
  | "eq"
  | "ne"
  | "gt"
  | "lt"
  | "ge"
  | "le"
  | "before"
  | "after";

// 연산자 이름(Dify API 이름) → 번역 키. 기호 연산자는 키로 쓰지 않는다.
const OPERATOR_KEYS: Partial<Record<string, OperatorKey>> = {
  is: "is",
  "is not": "isNot",
  contains: "contains",
  "not contains": "notContains",
  "start with": "startWith",
  "end with": "endWith",
  empty: "empty",
  "not empty": "notEmpty",
  in: "in",
  "not in": "notIn",
  "=": "eq",
  "≠": "ne",
  ">": "gt",
  "<": "lt",
  "≥": "ge",
  "≤": "le",
  before: "before",
  after: "after",
};

export interface VariableSlot {
  value: string;
  onChange: (value: string) => void;
  label: string;
  invalid: boolean;
}

/*
 * 메타데이터 필터 빌더(3단계 설계 7.5, 12.3). 모드(끔·직접 조건·자동), 조건 줄(필드·연산자·값), and/or.
 * 연산자는 kb_options.operators가 필드 타입마다 주는 것만 고른다. 값은 상수이고, renderVariable을 넘기면
 * (노드 폼) 변수도 고를 수 있다. 여러 지식 베이스면 부르는 쪽이 공통 필드만 넘긴다.
 */
export function MetadataFilterBuilder({
  value,
  onChange,
  fields,
  operators,
  modes = FILTER_MODES,
  maxConditions = MAX_FILTER_CONDITIONS,
  renderVariable,
  renderModel,
  isSelector,
  issueAt,
  fieldsHint,
  fieldsError,
  onRetryFields,
  disabled,
}: {
  value: MetadataFilterForm;
  onChange: (next: MetadataFilterForm) => void;
  // 고를 수 있는 필드. null이면 아직 읽는 중이거나 읽지 못했다(fieldsError).
  fields: readonly FilterField[] | null;
  operators: Record<string, readonly string[]>;
  modes?: readonly FilterMode[];
  maxConditions?: number;
  renderVariable?: (slot: VariableSlot) => ReactNode;
  // 자동 필터의 모델 칸
  renderModel?: (slot: { value: string; onChange: (value: string) => void }) => ReactNode;
  isSelector?: (text: string) => boolean;
  // 서버 검증 이슈(경로는 "conditions.0.name"처럼 metadata_filter 안쪽)
  issueAt?: (path: string) => string | undefined;
  fieldsHint?: ReactNode;
  fieldsError?: unknown;
  onRetryFields?: () => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const problems = conditionProblems(value, fields, operators, isSelector);
  const set = (patch: Partial<MetadataFilterForm>) => onChange({ ...value, ...patch });
  const setCondition = (index: number, patch: Partial<FilterConditionForm>) =>
    set({
      conditions: value.conditions.map((item, at) => (at === index ? { ...item, ...patch } : item)),
    });
  const known = fields ?? [];

  function chooseField(index: number, name: string) {
    const field = known.find((item) => item.name === name);
    const current = value.conditions[index];
    if (!field || !current) return;
    const allowed = operators[field.type] ?? [];
    const operator = allowed.includes(current.comparison_operator)
      ? current.comparison_operator
      : (allowed[0] ?? current.comparison_operator);
    setCondition(index, { name, type: field.type, comparison_operator: operator });
  }

  const modeIssue = issueAt?.("mode");
  return (
    <div className="flex flex-col gap-3">
      <Field label={t("filter.mode")} error={modeIssue}>
        <Select
          value={value.mode}
          disabled={disabled}
          onValueChange={(mode) => set({ mode: mode as FilterMode })}
          options={modes.map((mode) => ({
            value: mode,
            label: t(`filter.modes.${mode}`),
            hint: t(`filter.modeHints.${mode}`),
          }))}
        />
      </Field>
      {value.mode === "automatic" ? (
        <>
          <p className="text-xs text-ink-muted">{t("filter.automaticHint")}</p>
          {renderModel
            ? renderModel({
                value: value.model.model_id,
                onChange: (model_id) => set({ model: { model_id } }),
              })
            : null}
        </>
      ) : null}
      {value.mode === "manual" ? (
        <div className="flex flex-col gap-2">
          {fieldsHint ? <p className="text-xs text-ink-muted">{fieldsHint}</p> : null}
          {fieldsError ? (
            <ErrorAlert
              error={fieldsError}
              title={t("filter.loadFailed")}
              onRetry={onRetryFields}
            />
          ) : fields === null ? (
            <p className="text-xs text-ink-muted" role="status">
              {t("filter.loadingFields")}
            </p>
          ) : null}
          {fields !== null && known.length === 0 ? (
            <p className="rounded-md border border-dashed border-line px-3 py-2 text-xs text-ink-muted">
              {t("filter.noFields")}
            </p>
          ) : null}
          {value.conditions.length > 1 ? (
            <Field label={t("filter.logical")} inline>
              <Select
                aria-label={t("filter.logical")}
                className="h-7 w-28 text-xs"
                value={value.logical_operator}
                disabled={disabled}
                onValueChange={(logical) =>
                  set({ logical_operator: logical === "or" ? "or" : "and" })
                }
                options={[
                  { value: "and", label: t("filter.and") },
                  { value: "or", label: t("filter.or") },
                ]}
              />
            </Field>
          ) : null}
          <ol className="flex flex-col gap-2">
            {value.conditions.map((condition, index) => {
              const row = t("filter.row", { n: index + 1 });
              const found = problems[index] ?? {};
              const options: SelectOption[] = known.map((field) => ({
                value: field.name,
                label: field.name,
                hint: t(`metadata.types.${field.type}`),
              }));
              if (condition.name && !known.some((field) => field.name === condition.name)) {
                options.push({
                  value: condition.name,
                  label: condition.name,
                  // 필드 목록을 읽기 전에는 없는 필드인지 아직 모른다.
                  hint: fields !== null ? t("filter.missingField") : undefined,
                });
              }
              const type = conditionType(condition, fields);
              const allowed = operators[type] ?? [];
              const operatorOptions = (
                allowed.includes(condition.comparison_operator)
                  ? allowed
                  : [condition.comparison_operator, ...allowed]
              ).map((operator) => {
                const key = OPERATOR_KEYS[operator];
                return { value: operator, label: key ? t(`filter.ops.${key}`) : operator };
              });
              const needsValue = !NO_VALUE_OPERATORS.has(condition.comparison_operator);
              const nameError =
                (found.name ? t(`filter.problems.${found.name}`) : undefined) ??
                issueAt?.(`conditions.${index}.name`);
              const valueError =
                (found.value ? t(`filter.problems.${found.value}`) : undefined) ??
                issueAt?.(`conditions.${index}`) ??
                issueAt?.(`conditions.${index}.value.var`);
              return (
                <li
                  key={index}
                  aria-label={row}
                  className="flex flex-col gap-1.5 rounded-md border border-line p-2"
                >
                  <div className="flex items-center gap-1.5">
                    <Select
                      aria-label={t("filter.field", { row })}
                      className="h-7 min-w-0 flex-1 text-xs"
                      value={condition.name || undefined}
                      placeholder={t("filter.pickField")}
                      disabled={disabled}
                      onValueChange={(name) => chooseField(index, name)}
                      options={options}
                    />
                    <Select
                      aria-label={t("filter.operator", { row })}
                      className="h-7 w-32 text-xs"
                      value={condition.comparison_operator}
                      disabled={disabled}
                      onValueChange={(operator) =>
                        setCondition(index, { comparison_operator: operator })
                      }
                      options={operatorOptions}
                    />
                    <Button
                      size="iconSm"
                      variant="dangerGhost"
                      aria-label={t("filter.remove", { row })}
                      title={t("filter.remove", { row })}
                      disabled={disabled}
                      onClick={() =>
                        set({ conditions: value.conditions.filter((_, at) => at !== index) })
                      }
                    >
                      <Trash2 className="size-4" aria-hidden />
                    </Button>
                  </div>
                  {nameError ? (
                    <p className="text-xs text-danger" role="alert">
                      {nameError}
                    </p>
                  ) : null}
                  {found.comparison_operator ? (
                    <p className="text-xs text-danger" role="alert">
                      {t("filter.problems.operator")}
                    </p>
                  ) : null}
                  {needsValue ? (
                    <div className="flex items-center gap-1.5">
                      {renderVariable ? (
                        <Select
                          aria-label={t("filter.source", { row })}
                          className="h-7 w-24 shrink-0 text-xs"
                          value={condition.source}
                          disabled={disabled}
                          onValueChange={(source) =>
                            setCondition(index, {
                              source: source === "var" ? "var" : "const",
                              value_text: "",
                              var: "",
                            })
                          }
                          options={[
                            { value: "const", label: t("filter.constant") },
                            { value: "var", label: t("filter.variable") },
                          ]}
                        />
                      ) : null}
                      {renderVariable && condition.source === "var" ? (
                        <div className="min-w-0 flex-1">
                          {renderVariable({
                            value: condition.var,
                            onChange: (selector) => setCondition(index, { var: selector }),
                            label: t("filter.value", { row }),
                            invalid: Boolean(valueError),
                          })}
                        </div>
                      ) : (
                        <Input
                          aria-label={t("filter.value", { row })}
                          aria-invalid={Boolean(valueError) || undefined}
                          className="h-7 min-w-0 flex-1 text-xs"
                          value={condition.value_text}
                          disabled={disabled}
                          placeholder={
                            LIST_OPERATORS.has(condition.comparison_operator)
                              ? t("filter.listPlaceholder")
                              : type === "time"
                                ? t("filter.timePlaceholder")
                                : undefined
                          }
                          onChange={(event) =>
                            setCondition(index, { value_text: event.target.value })
                          }
                        />
                      )}
                    </div>
                  ) : null}
                  {needsValue && valueError ? (
                    <p className="text-xs text-danger" role="alert">
                      {valueError}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ol>
          <div>
            <Button
              size="sm"
              variant="secondary"
              disabled={disabled || known.length === 0 || value.conditions.length >= maxConditions}
              onClick={() =>
                set({ conditions: [...value.conditions, newCondition(known[0], operators)] })
              }
            >
              <Plus className="size-3.5" aria-hidden />
              {t("filter.add")}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
