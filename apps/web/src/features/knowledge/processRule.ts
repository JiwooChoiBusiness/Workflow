/*
 * 정리·청크 규칙(설계 6.3) 폼. 서버 ProcessRule(app/schemas/knowledge.py)과 같은 기본값·범위로 검사한다.
 * 구분자는 개행·탭·공백이 값이라 화면에서는 \n, \t, ␣로 바꿔 보인다.
 */
import type { DocForm, ProcessRule } from "@/api/types";

export const MIN_CHUNK_TOKENS = 20;
export const MAX_CHUNK_TOKENS = 4000;
export const MAX_SEPARATORS = 10;
export const MAX_SEPARATOR_LENGTH = 16;

export interface RuleValues {
  cleaning: { remove_extra_spaces: boolean; remove_urls_emails: boolean };
  general: { separators: string[]; max_tokens: number; overlap_tokens: number };
  parent: { mode: "paragraph" | "full_doc"; separators: string[]; max_tokens: number };
  child: { separators: string[]; max_tokens: number };
}

export const DEFAULT_RULE: RuleValues = {
  cleaning: { remove_extra_spaces: true, remove_urls_emails: false },
  general: { separators: ["\n\n", "\n", ". ", " "], max_tokens: 1024, overlap_tokens: 50 },
  parent: { mode: "paragraph", separators: ["\n\n"], max_tokens: 1024 },
  child: { separators: ["\n", ". "], max_tokens: 256 },
};

// 서버 응답의 규칙은 칸이 빠져 있을 수 있어(스키마 기본값) 폼이 쓰기 전에 모두 채운다.
export function completeRule(rule: ProcessRule | null | undefined): RuleValues {
  return {
    cleaning: { ...DEFAULT_RULE.cleaning, ...rule?.cleaning },
    general: {
      ...DEFAULT_RULE.general,
      ...rule?.general,
      separators: rule?.general?.separators ?? DEFAULT_RULE.general.separators,
    },
    parent: {
      ...DEFAULT_RULE.parent,
      ...rule?.parent,
      separators: rule?.parent?.separators ?? DEFAULT_RULE.parent.separators,
    },
    child: {
      ...DEFAULT_RULE.child,
      ...rule?.child,
      separators: rule?.child?.separators ?? DEFAULT_RULE.child.separators,
    },
  };
}

export function sameRule(a: ProcessRule | null | undefined, b: ProcessRule | null | undefined) {
  return JSON.stringify(completeRule(a)) === JSON.stringify(completeRule(b));
}

const ESCAPES: [string, string][] = [
  ["\\", "\\\\"],
  ["\n", "\\n"],
  ["\t", "\\t"],
  [" ", "␣"],
];

export function escapeSeparator(value: string): string {
  return ESCAPES.reduce((text, [raw, shown]) => text.split(raw).join(shown), value);
}

// 사용자가 입력칸에 쓴 \n·\t·␣를 실제 문자로 바꾼다. 그냥 친 공백도 공백이다.
export function unescapeSeparator(text: string): string {
  let result = "";
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === "␣") {
      result += " ";
    } else if (char === "\\" && index + 1 < text.length) {
      const next = text[index + 1];
      if (next === "n" || next === "t" || next === "\\") {
        result += next === "n" ? "\n" : next === "t" ? "\t" : "\\";
        index += 1;
      } else {
        result += char;
      }
    } else {
      result += char;
    }
  }
  return result;
}

export type RuleField =
  | "general.separators"
  | "general.max_tokens"
  | "general.overlap_tokens"
  | "parent.separators"
  | "parent.max_tokens"
  | "child.separators"
  | "child.max_tokens";

export type RuleError = "tokens" | "overlap" | "childOverParent" | "separators";

function tokensInvalid(value: number): boolean {
  return !Number.isInteger(value) || value < MIN_CHUNK_TOKENS || value > MAX_CHUNK_TOKENS;
}

function separatorsInvalid(values: readonly string[]): boolean {
  return (
    values.length < 1 ||
    values.length > MAX_SEPARATORS ||
    values.some((value) => value.length < 1 || value.length > MAX_SEPARATOR_LENGTH)
  );
}

// 그 청크 모드가 쓰는 칸만 본다. Q&A는 나누지 않아 검사할 것이 없다.
export function ruleErrors(
  rule: RuleValues,
  docForm: DocForm,
): Partial<Record<RuleField, RuleError>> {
  const errors: Partial<Record<RuleField, RuleError>> = {};
  if (docForm === "text_model") {
    const { general } = rule;
    if (separatorsInvalid(general.separators)) errors["general.separators"] = "separators";
    if (tokensInvalid(general.max_tokens)) errors["general.max_tokens"] = "tokens";
    if (
      !Number.isInteger(general.overlap_tokens) ||
      general.overlap_tokens < 0 ||
      general.overlap_tokens >= general.max_tokens
    ) {
      errors["general.overlap_tokens"] = "overlap";
    }
  } else if (docForm === "hierarchical_model") {
    const { parent, child } = rule;
    if (separatorsInvalid(parent.separators)) errors["parent.separators"] = "separators";
    if (parent.mode === "paragraph" && tokensInvalid(parent.max_tokens)) {
      errors["parent.max_tokens"] = "tokens";
    }
    if (separatorsInvalid(child.separators)) errors["child.separators"] = "separators";
    if (tokensInvalid(child.max_tokens)) {
      errors["child.max_tokens"] = "tokens";
    } else if (parent.mode === "paragraph" && child.max_tokens > parent.max_tokens) {
      errors["child.max_tokens"] = "childOverParent";
    }
  }
  return errors;
}

export function toProcessRule(rule: RuleValues): ProcessRule {
  return {
    cleaning: { ...rule.cleaning },
    general: { ...rule.general, separators: [...rule.general.separators] },
    parent: { ...rule.parent, separators: [...rule.parent.separators] },
    child: { ...rule.child, separators: [...rule.child.separators] },
  };
}
