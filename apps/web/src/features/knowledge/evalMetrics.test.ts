import { describe, expect, it } from "vitest";
import type { EvalCaseResult } from "@/api/types";
import {
  changeCounts,
  compareCases,
  compareMetrics,
  formatDelta,
  formatMetric,
  rankBuckets,
  settingsDifferences,
} from "./evalMetrics";
import { DEFAULT_RETRIEVAL } from "./retrievalSettings";

function result(caseId: string, rank: number | null): EvalCaseResult {
  return { case_id: caseId, question: `질문 ${caseId}`, rank, top: [] };
}

describe("평가 지표", () => {
  it("지표를 소수 셋째 자리로, 차이는 부호와 함께 보인다", () => {
    expect(formatMetric(0.98113)).toBe("0.981");
    expect(formatMetric(null)).toBe("-");
    expect(formatDelta(0.0189)).toBe("+0.019");
    expect(formatDelta(-0.0566)).toBe("−0.057");
    expect(formatDelta(0.0001)).toBe("±0");
  });

  it("두 실행의 지표 차이를 R@1·R@3·R@5·MRR@10 순으로 낸다", () => {
    const before = { r1: 0.8, r3: 0.9, r5: 0.95, mrr10: 0.85, cases: 20 };
    const after = { r1: 0.85, r3: 0.9, r5: 0.9, mrr10: 0.88, cases: 20 };
    const deltas = compareMetrics(before, after);
    expect(deltas.map((item) => item.key)).toEqual(["r1", "r3", "r5", "mrr10"]);
    expect(deltas[0].delta).toBeCloseTo(0.05);
    expect(deltas[2].delta).toBeCloseTo(-0.05);
  });

  it("문항별 순위 변화는 낮을수록 좋고, 10위 밖(null)은 가장 나쁘다", () => {
    const rows = compareCases(
      [result("a", 3), result("b", 1), result("c", null), result("gone", 2)],
      [result("a", 1), result("b", 2), result("c", 5), result("new", 1)],
    );
    expect(rows.map((row) => [row.caseId, row.change])).toEqual([
      ["a", "better"],
      ["b", "worse"],
      ["c", "better"],
      ["new", "added"],
      ["gone", "removed"],
    ]);
    expect(changeCounts(rows)).toEqual({ better: 2, worse: 1, same: 0, added: 1, removed: 1 });
  });

  it("순위를 1위·2~3위·4~10위·놓침으로 센다", () => {
    expect(
      rankBuckets([result("a", 1), result("b", 3), result("c", 7), result("d", null)]),
    ).toEqual({
      top1: 1,
      top3: 1,
      top10: 1,
      missed: 1,
    });
  });

  it("두 실행의 검색 설정에서 다른 칸만 뽑는다", () => {
    const changed = {
      ...DEFAULT_RETRIEVAL,
      reranking_mode: "rrf" as const,
      weights: { vector_weight: 0.8, keyword_weight: 0.2 },
    };
    expect(settingsDifferences(DEFAULT_RETRIEVAL, changed)).toEqual([
      { key: "reranking_mode", before: "weighted_score", after: "rrf" },
      { key: "vector_weight", before: 0.7, after: 0.8 },
    ]);
    expect(settingsDifferences(DEFAULT_RETRIEVAL, DEFAULT_RETRIEVAL)).toEqual([]);
  });
});
