import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  LIMIT_KEYS,
  LIMIT_RANGES,
  type LimitKey,
  type LimitProblem,
  type LimitsDraft,
} from "@/features/sql/limits";

// 데이터 소스 한도 다섯 칸(만들기 대화상자, 설정 탭). 값은 글로 받고 저장할 때 parseLimits가 검사한다.
export function SqlLimitsFields({
  value,
  onChange,
  problems,
  maxRowsCap,
  disabled,
}: {
  value: LimitsDraft;
  onChange: (next: LimitsDraft) => void;
  problems: Partial<Record<LimitKey, LimitProblem>>;
  // SQL_MAX_ROWS(노드 옵션 max_rows_max). 저장은 더 큰 값도 받지만 실행 때 이 값까지만 받는다.
  maxRowsCap: number;
  disabled?: boolean;
}) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const hints: Record<LimitKey, string> = {
    max_rows: t("limits.max_rowsHint", { max: maxRowsCap }),
    timeout_sec: t("limits.timeout_secHint", { max: LIMIT_RANGES.timeout_sec[1] }),
    memory_mb: t("limits.memory_mbHint"),
    llm_rows: t("limits.llm_rowsHint"),
    llm_bytes: t("limits.llm_bytesHint", { max: LIMIT_RANGES.llm_bytes[1] }),
  };
  function problemText(key: LimitKey): string | undefined {
    const problem = problems[key];
    if (!problem) return undefined;
    const [min, max] = LIMIT_RANGES[key];
    return problem === "integer"
      ? tc("validation.integer")
      : `${tc("validation.min", { min })} · ${tc("validation.max", { max })}`;
  }
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {LIMIT_KEYS.map((key) => (
        <Field key={key} label={t(`limits.${key}`)} hint={hints[key]} error={problemText(key)}>
          <Input
            type="number"
            inputMode="numeric"
            step="1"
            min={LIMIT_RANGES[key][0]}
            max={LIMIT_RANGES[key][1]}
            className="w-36"
            value={value[key]}
            disabled={disabled}
            onChange={(event) => onChange({ ...value, [key]: event.target.value })}
          />
        </Field>
      ))}
    </div>
  );
}
