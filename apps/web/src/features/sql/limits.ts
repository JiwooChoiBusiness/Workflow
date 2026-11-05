import type { SqlLimits, SqlNodeOptions } from "@/api/types";

export type LimitKey = keyof SqlLimits;

export const LIMIT_KEYS: readonly LimitKey[] = [
  "max_rows",
  "timeout_sec",
  "memory_mb",
  "llm_rows",
  "llm_bytes",
];

// 서버 SqlLimits(app/schemas/sql_source.py)의 범위. 행 상한은 SQL_MAX_ROWS가 더 낮으면 실행 때 그 값이다.
export const LIMIT_RANGES: Record<LimitKey, readonly [number, number]> = {
  max_rows: [1, 10_000],
  timeout_sec: [1, 60],
  memory_mb: [128, 8192],
  llm_rows: [1, 10_000],
  llm_bytes: [1024, 65_536],
};

// 서버 설정의 기본값(SQL_TIMEOUT_SEC 등을 고치지 않았을 때). 노드 옵션(sql_options)을 아직 못 읽었을 때만 쓴다.
export const DEFAULT_LIMITS: SqlLimits = {
  max_rows: 200,
  timeout_sec: 30,
  memory_mb: 512,
  llm_rows: 50,
  llm_bytes: 65_536,
};

// 새 데이터 소스의 한도. 다섯 값 모두 서버가 설정에서 만든 sql_options의 기본값이다.
export function defaultLimits(options: SqlNodeOptions | null | undefined): SqlLimits {
  if (!options) return DEFAULT_LIMITS;
  return {
    max_rows: options.default_max_rows,
    timeout_sec: options.default_timeout_sec,
    memory_mb: options.default_memory_mb,
    llm_rows: options.default_llm_rows,
    llm_bytes: options.default_llm_bytes,
  };
}

// 입력 칸의 글. 빈칸·소수·범위 밖은 parseLimits가 칸 오류로 돌려준다.
export type LimitsDraft = Record<LimitKey, string>;

export function limitsDraft(limits: SqlLimits): LimitsDraft {
  return Object.fromEntries(LIMIT_KEYS.map((key) => [key, String(limits[key])])) as LimitsDraft;
}

export type LimitProblem = "integer" | "range";

export function parseLimits(draft: LimitsDraft): {
  limits: SqlLimits | null;
  problems: Partial<Record<LimitKey, LimitProblem>>;
} {
  const problems: Partial<Record<LimitKey, LimitProblem>> = {};
  const values: Partial<SqlLimits> = {};
  for (const key of LIMIT_KEYS) {
    const text = draft[key].trim();
    const value = Number(text);
    if (!text || !Number.isInteger(value)) {
      problems[key] = "integer";
      continue;
    }
    const [min, max] = LIMIT_RANGES[key];
    if (value < min || value > max) problems[key] = "range";
    values[key] = value;
  }
  return Object.keys(problems).length > 0
    ? { limits: null, problems }
    : { limits: values as SqlLimits, problems };
}

export function sameLimits(a: SqlLimits, b: SqlLimits): boolean {
  return LIMIT_KEYS.every((key) => a[key] === b[key]);
}
