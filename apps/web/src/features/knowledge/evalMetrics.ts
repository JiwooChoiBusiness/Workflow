/*
 * 평가 실행 결과(R@1·R@3·R@5·MRR@10, 설계 7.7)를 읽고 두 실행을 비교한다. 순위는 정답이 처음 나온 자리(1부터)이고
 * 상위 10개 안에 없으면 null이다.
 */
import type { EvalCaseResult, EvalMetrics, RetrievalSettings } from "@/api/types";
import { toRetrievalForm, type RetrievalForm } from "./retrievalSettings";

export const METRIC_KEYS = ["r1", "r3", "r5", "mrr10"] as const;
export type MetricKey = (typeof METRIC_KEYS)[number];

export function formatMetric(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : value.toFixed(3);
}

export function formatDelta(delta: number): string {
  if (Math.abs(delta) < 0.0005) return "±0";
  return `${delta > 0 ? "+" : "−"}${Math.abs(delta).toFixed(3)}`;
}

export interface MetricDelta {
  key: MetricKey;
  before: number;
  after: number;
  delta: number;
}

export function compareMetrics(before: EvalMetrics, after: EvalMetrics): MetricDelta[] {
  return METRIC_KEYS.map((key) => ({
    key,
    before: before[key],
    after: after[key],
    delta: after[key] - before[key],
  }));
}

export type RankChange = "better" | "worse" | "same" | "added" | "removed";

export interface CaseComparison {
  caseId: string;
  question: string;
  before: number | null;
  after: number | null;
  change: RankChange;
}

// 상위 10개 밖(null)은 가장 나쁜 순위로 본다.
function rankValue(rank: number | null | undefined): number {
  return rank ?? Number.POSITIVE_INFINITY;
}

export function compareCases(
  before: readonly EvalCaseResult[],
  after: readonly EvalCaseResult[],
): CaseComparison[] {
  const earlier = new Map(before.map((item) => [item.case_id, item]));
  const rows: CaseComparison[] = after.map((item) => {
    const old = earlier.get(item.case_id);
    earlier.delete(item.case_id);
    if (!old) {
      return {
        caseId: item.case_id,
        question: item.question,
        before: null,
        after: item.rank ?? null,
        change: "added",
      };
    }
    const a = rankValue(old.rank);
    const b = rankValue(item.rank);
    return {
      caseId: item.case_id,
      question: item.question,
      before: old.rank ?? null,
      after: item.rank ?? null,
      change: b < a ? "better" : b > a ? "worse" : "same",
    };
  });
  for (const old of earlier.values()) {
    rows.push({
      caseId: old.case_id,
      question: old.question,
      before: old.rank ?? null,
      after: null,
      change: "removed",
    });
  }
  return rows;
}

export function changeCounts(rows: readonly CaseComparison[]): Record<RankChange, number> {
  const counts: Record<RankChange, number> = {
    better: 0,
    worse: 0,
    same: 0,
    added: 0,
    removed: 0,
  };
  for (const row of rows) counts[row.change] += 1;
  return counts;
}

export interface RankBuckets {
  top1: number;
  top3: number;
  top10: number;
  missed: number;
}

export function rankBuckets(cases: readonly Pick<EvalCaseResult, "rank">[]): RankBuckets {
  const buckets: RankBuckets = { top1: 0, top3: 0, top10: 0, missed: 0 };
  for (const item of cases) {
    const rank = item.rank;
    if (rank == null) buckets.missed += 1;
    else if (rank === 1) buckets.top1 += 1;
    else if (rank <= 3) buckets.top3 += 1;
    else buckets.top10 += 1;
  }
  return buckets;
}

export type SettingsKey = keyof RetrievalForm;

export interface SettingsDifference {
  key: SettingsKey;
  before: RetrievalForm[SettingsKey];
  after: RetrievalForm[SettingsKey];
}

// 비교 화면에서 두 실행의 검색 설정 중 다른 칸만 보인다. 결합 방식과 무관한 칸(재순위 후보 수 등)도 그대로 센다.
export function settingsDifferences(
  before: RetrievalSettings,
  after: RetrievalSettings,
): SettingsDifference[] {
  const a = toRetrievalForm(before);
  const b = toRetrievalForm(after);
  return (Object.keys(a) as SettingsKey[])
    .filter((key) => a[key] !== b[key])
    .map((key) => ({ key, before: a[key], after: b[key] }));
}
