import { describe, expect, it } from "vitest";
import {
  completeRule,
  DEFAULT_RULE,
  escapeSeparator,
  ruleErrors,
  sameRule,
  toProcessRule,
  unescapeSeparator,
} from "./processRule";

describe("청크 규칙", () => {
  it("빠진 칸을 서버 기본값으로 채운다", () => {
    expect(completeRule(null)).toEqual(DEFAULT_RULE);
    const partial = completeRule({ general: { max_tokens: 500, overlap_tokens: 20 } });
    expect(partial.general).toEqual({
      separators: DEFAULT_RULE.general.separators,
      max_tokens: 500,
      overlap_tokens: 20,
    });
    expect(sameRule(toProcessRule(DEFAULT_RULE), {})).toBe(true);
    expect(sameRule({ child: { max_tokens: 128 } }, {})).toBe(false);
  });

  it("구분자의 줄바꿈·탭·공백을 보이는 글자로 바꾸고 되돌린다", () => {
    expect(escapeSeparator("\n\n")).toBe("\\n\\n");
    expect(escapeSeparator(". ")).toBe(".␣");
    expect(escapeSeparator("\t")).toBe("\\t");
    for (const value of ["\n\n", "\n", ". ", " ", "\t", "\\x"]) {
      expect(unescapeSeparator(escapeSeparator(value))).toBe(value);
    }
    expect(unescapeSeparator("\\n ")).toBe("\n ");
    expect(unescapeSeparator("\\q")).toBe("\\q");
  });

  it("그 청크 모드가 쓰는 칸만 서버와 같은 범위로 검사한다", () => {
    const bad = {
      ...DEFAULT_RULE,
      general: { separators: [], max_tokens: 10, overlap_tokens: 50 },
      child: { separators: ["\n"], max_tokens: 2000 },
    };
    expect(ruleErrors(bad, "text_model")).toEqual({
      "general.separators": "separators",
      "general.max_tokens": "tokens",
      "general.overlap_tokens": "overlap",
    });
    expect(ruleErrors(bad, "hierarchical_model")).toEqual({
      "child.max_tokens": "childOverParent",
    });
    expect(
      ruleErrors(
        { ...bad, parent: { ...bad.parent, mode: "full_doc", max_tokens: 1 } },
        "hierarchical_model",
      ),
    ).toEqual({});
    expect(ruleErrors(bad, "qa_model")).toEqual({});
    expect(ruleErrors(DEFAULT_RULE, "text_model")).toEqual({});
  });
});
