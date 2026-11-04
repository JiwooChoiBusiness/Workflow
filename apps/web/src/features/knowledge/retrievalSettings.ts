/*
 * 검색 설정(설계 7장)의 폼 값과 서버 값 사이 변환, 검사, 검색 시험 결과 점수의 종류. 서버 기본값은
 * app/schemas/knowledge.py의 RetrievalSettings와 같다.
 */
import type {
  IndexingTechnique,
  RerankingMode,
  RetrievalSettings,
  RetrievalWarning,
  RetrieveRecord,
  SearchMethod,
} from "@/api/types";

export const SEARCH_METHODS: readonly SearchMethod[] = [
  "hybrid_search",
  "semantic_search",
  "full_text_search",
];
export const RERANKING_MODES: readonly RerankingMode[] = ["weighted_score", "rrf", "llm"];
export const RERANK_CANDIDATES_MAX = 20;

export const DEFAULT_RETRIEVAL: RetrievalSettings = {
  search_method: "hybrid_search",
  top_k: 4,
  score_threshold_enabled: false,
  score_threshold: 0.5,
  reranking_mode: "weighted_score",
  weights: { vector_weight: 0.7, keyword_weight: 0.3 },
  rerank: { model_id: null, candidates: 10 },
};

// 폼은 평평하게 둔다. 키워드 가중치는 1 - 벡터 가중치라 따로 두지 않는다(서버도 합을 1로 맞춘다).
export interface RetrievalForm {
  search_method: SearchMethod;
  top_k: number;
  score_threshold_enabled: boolean;
  score_threshold: number;
  reranking_mode: RerankingMode;
  vector_weight: number;
  rerank_candidates: number;
  rerank_model_id: string;
}

function round(value: number, digits = 2): number {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

export function toRetrievalForm(settings: RetrievalSettings | null | undefined): RetrievalForm {
  const value = settings ?? DEFAULT_RETRIEVAL;
  const vector = value.weights?.vector_weight ?? 0.7;
  const keyword = value.weights?.keyword_weight ?? 0.3;
  const total = vector + keyword;
  return {
    search_method: value.search_method,
    top_k: value.top_k,
    score_threshold_enabled: value.score_threshold_enabled,
    score_threshold: value.score_threshold,
    reranking_mode: value.reranking_mode,
    vector_weight: total > 0 ? round(vector / total) : 0.7,
    rerank_candidates: value.rerank?.candidates ?? 10,
    rerank_model_id: value.rerank?.model_id ?? "",
  };
}

export function fromRetrievalForm(form: RetrievalForm): RetrievalSettings {
  const vector = round(Math.min(1, Math.max(0, form.vector_weight)));
  return {
    search_method: form.search_method,
    top_k: form.top_k,
    score_threshold_enabled: form.score_threshold_enabled,
    score_threshold: form.score_threshold,
    reranking_mode: form.reranking_mode,
    weights: { vector_weight: vector, keyword_weight: round(1 - vector) },
    rerank: { model_id: form.rerank_model_id.trim() || null, candidates: form.rerank_candidates },
  };
}

// 서버 RetrievalSettings의 칸 경로(["weights", "vector_weight"], ["rerank", "candidates"])를 폼 칸으로 옮긴다.
// 폼은 평평하고 키워드 가중치 칸이 없어 weights 아래 오류는 벡터 가중치 칸에 붙인다.
export function retrievalFormField(path: readonly (string | number)[]): keyof RetrievalForm | null {
  const [head, sub] = path.map(String);
  switch (head) {
    case "search_method":
    case "top_k":
    case "score_threshold":
    case "reranking_mode":
      return head;
    case "score_threshold_enabled":
      return "score_threshold";
    case "weights":
      return "vector_weight";
    case "rerank":
      return sub === "model_id" ? "rerank_model_id" : "rerank_candidates";
    default:
      return null;
  }
}

// economy KB에는 벡터가 없어 전문 검색만 고를 수 있다(서버 search_method_unavailable).
export function methodsFor(technique: IndexingTechnique): readonly SearchMethod[] {
  return technique === "economy" ? ["full_text_search"] : SEARCH_METHODS;
}

export type RetrievalField = "top_k" | "score_threshold" | "vector_weight" | "rerank_candidates";

// 폼 칸 → 번역 키(retrieval.errors.*). 서버 검사(knowledge.py _check_retrieval, 스키마 범위)와 같다.
export function retrievalErrors(
  form: RetrievalForm,
  topKMax: number,
  candidatesMax: number = RERANK_CANDIDATES_MAX,
): Partial<Record<RetrievalField, "topK" | "threshold" | "weight" | "candidates">> {
  const errors: Partial<Record<RetrievalField, "topK" | "threshold" | "weight" | "candidates">> =
    {};
  if (!Number.isInteger(form.top_k) || form.top_k < 1 || form.top_k > topKMax) {
    errors.top_k = "topK";
  }
  if (!(form.score_threshold >= 0 && form.score_threshold <= 1)) {
    errors.score_threshold = "threshold";
  }
  if (!(form.vector_weight >= 0 && form.vector_weight <= 1)) errors.vector_weight = "weight";
  if (
    !Number.isInteger(form.rerank_candidates) ||
    form.rerank_candidates < 1 ||
    form.rerank_candidates > candidatesMax
  ) {
    errors.rerank_candidates = "candidates";
  }
  return errors;
}

export function retrievalFormValid(
  form: RetrievalForm,
  topKMax: number,
  candidatesMax: number = RERANK_CANDIDATES_MAX,
): boolean {
  return Object.keys(retrievalErrors(form, topKMax, candidatesMax)).length === 0;
}

export function sameRetrieval(
  a: RetrievalSettings | null | undefined,
  b: RetrievalSettings | null | undefined,
): boolean {
  return (
    JSON.stringify(fromRetrievalForm(toRetrievalForm(a))) ===
    JSON.stringify(fromRetrievalForm(toRetrievalForm(b)))
  );
}

// 결과 카드의 점수가 무엇인지(설계 17.4 "M23 이후 확정"의 점수 규칙). 벡터만이면 코사인 원값, 전문만이면
// bm25 / 목록 최대값, 하이브리드는 min-max 가중합이나 RRF, LLM 재순위면 모델이 준 관련도다. 질의 임베딩이
// 실패했거나 벡터 색인이 없어 전문 검색으로 돈 결과는 bm25 기준이다.
export type ScoreKind = "cosine" | "weighted" | "rrf" | "llm" | "keyword";

const SAME = 1e-9;

export function recordScoreKind(
  record: Pick<RetrieveRecord, "score" | "fusion" | "fusion_score">,
  settings: Pick<RetrievalSettings, "search_method" | "reranking_mode">,
  technique: IndexingTechnique,
  warnings: readonly Pick<RetrievalWarning, "code">[] = [],
): ScoreKind {
  const codes = new Set(warnings.map((warning) => warning.code));
  if (
    settings.reranking_mode === "llm" &&
    settings.search_method === "hybrid_search" &&
    technique === "high_quality" &&
    !codes.has("rerank_failed") &&
    record.fusion_score != null
  ) {
    return "llm";
  }
  const fusion = record.fusion;
  const keywordOnly =
    fusion != null &&
    fusion.cosine == null &&
    fusion.vector_score == null &&
    fusion.keyword_score != null &&
    Math.abs(record.score - fusion.keyword_score) < SAME;
  if (
    technique === "economy" ||
    settings.search_method === "full_text_search" ||
    codes.has("vector_unavailable") ||
    keywordOnly
  ) {
    return "keyword";
  }
  if (settings.search_method === "semantic_search") return "cosine";
  return settings.reranking_mode === "rrf" ? "rrf" : "weighted";
}
