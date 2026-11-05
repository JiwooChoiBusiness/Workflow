import { describe, expect, it } from "vitest";
import type { SqlNodeOptions } from "@/api/types";
import nodeTypeList from "@/test/fixtures/contract/node-types.json";
import { DEFAULT_LIMITS, defaultLimits, limitsDraft, parseLimits, sameLimits } from "./limits";

// .env에서 SQL_TIMEOUT_SEC·SQL_MEMORY_MB 등을 바꾼 서버가 주는 노드 옵션.
const changed: SqlNodeOptions = {
  ...(nodeTypeList.sql_options as SqlNodeOptions),
  default_max_rows: 150,
  default_timeout_sec: 12,
  default_memory_mb: 1024,
  default_llm_rows: 40,
  default_llm_bytes: 32_768,
};

describe("데이터 소스 한도의 기본값", () => {
  it("다섯 값 모두 서버 설정에서 만든 sql_options를 따른다", () => {
    expect(defaultLimits(changed)).toEqual({
      max_rows: 150,
      timeout_sec: 12,
      memory_mb: 1024,
      llm_rows: 40,
      llm_bytes: 32_768,
    });
  });

  it("옵션을 아직 못 읽었으면 서버 설정의 기본값과 같은 상수를 쓴다", () => {
    expect(defaultLimits(null)).toBe(DEFAULT_LIMITS);
    expect(defaultLimits(nodeTypeList.sql_options as SqlNodeOptions)).toEqual(DEFAULT_LIMITS);
  });

  it("다른 칸만 고쳐도 메모리는 서버 설정값으로 남는다(상수 512가 섞이지 않는다)", () => {
    const initial = defaultLimits(changed);
    const parsed = parseLimits({ ...limitsDraft(initial), max_rows: "300" }).limits;

    expect(parsed).toEqual({ ...initial, max_rows: 300 });
    expect(parsed?.memory_mb).toBe(1024);
    expect(parsed && sameLimits(parsed, initial)).toBe(false);
  });
});
