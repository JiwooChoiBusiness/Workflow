import { describe, expect, it } from "vitest";
import type { RetrievalSettings } from "@/api/types";
import {
  DEFAULT_RETRIEVAL,
  fromRetrievalForm,
  methodsFor,
  recordScoreKind,
  retrievalErrors,
  retrievalFormField,
  sameRetrieval,
  toRetrievalForm,
} from "./retrievalSettings";

const hybrid: RetrievalSettings = { ...DEFAULT_RETRIEVAL };

describe("검색 설정 폼", () => {
  it("서버 값과 폼 값을 오가고 키워드 가중치는 1 - 벡터 가중치다", () => {
    const form = toRetrievalForm({
      ...hybrid,
      weights: { vector_weight: 0.8, keyword_weight: 0.2 },
      rerank: { model_id: "gemini-3.6-flash", candidates: 12 },
    });
    expect(form.vector_weight).toBe(0.8);
    expect(form.rerank_model_id).toBe("gemini-3.6-flash");
    const back = fromRetrievalForm({ ...form, vector_weight: 0.65, rerank_model_id: "  " });
    expect(back.weights).toEqual({ vector_weight: 0.65, keyword_weight: 0.35 });
    expect(back.rerank).toEqual({ model_id: null, candidates: 12 });
    expect(toRetrievalForm(null)).toEqual(toRetrievalForm(DEFAULT_RETRIEVAL));
  });

  it("합이 1이 아닌 가중치는 맞춰 읽고, 같은 뜻의 설정은 같다고 본다", () => {
    const odd = { ...hybrid, weights: { vector_weight: 1.4, keyword_weight: 0.6 } };
    expect(toRetrievalForm(odd).vector_weight).toBe(0.7);
    expect(sameRetrieval(odd, hybrid)).toBe(true);
    expect(sameRetrieval({ ...hybrid, top_k: 5 }, hybrid)).toBe(false);
  });

  it("Top K 상한, 문턱·가중치 범위, 재순위 후보 수를 서버와 같게 본다", () => {
    const form = toRetrievalForm(hybrid);
    expect(retrievalErrors(form, 20)).toEqual({});
    expect(retrievalErrors({ ...form, top_k: 21 }, 20)).toEqual({ top_k: "topK" });
    expect(retrievalErrors({ ...form, top_k: 1.5 }, 20)).toEqual({ top_k: "topK" });
    expect(
      retrievalErrors(
        { ...form, score_threshold: 1.2, vector_weight: -0.1, rerank_candidates: 0 },
        20,
      ),
    ).toEqual({
      score_threshold: "threshold",
      vector_weight: "weight",
      rerank_candidates: "candidates",
    });
  });

  it("서버 오류 위치를 평평한 폼 칸으로 옮긴다", () => {
    expect(retrievalFormField(["top_k"])).toBe("top_k");
    expect(retrievalFormField(["weights", "vector_weight"])).toBe("vector_weight");
    expect(retrievalFormField(["weights", "keyword_weight"])).toBe("vector_weight");
    expect(retrievalFormField(["weights"])).toBe("vector_weight");
    expect(retrievalFormField(["rerank", "candidates"])).toBe("rerank_candidates");
    expect(retrievalFormField(["rerank", "model_id"])).toBe("rerank_model_id");
    expect(retrievalFormField(["score_threshold_enabled"])).toBe("score_threshold");
    expect(retrievalFormField([])).toBeNull();
  });

  it("경제 KB는 전문 검색만 고른다", () => {
    expect(methodsFor("economy")).toEqual(["full_text_search"]);
    expect(methodsFor("high_quality")).toHaveLength(3);
  });
});

describe("검색 시험 점수의 종류", () => {
  const vector = { cosine: 0.82, vector_score: 1, keyword_score: null };
  const both = { cosine: 0.82, vector_score: 1, keyword_score: 0.5 };
  const keyword = { cosine: null, vector_score: null, keyword_score: 0.9 };

  it("벡터는 코사인 원값, 하이브리드는 결합 점수나 RRF, LLM 재순위는 관련도다", () => {
    expect(
      recordScoreKind(
        { score: 0.82, fusion: vector },
        { ...hybrid, search_method: "semantic_search" },
        "high_quality",
      ),
    ).toBe("cosine");
    expect(recordScoreKind({ score: 0.85, fusion: both }, hybrid, "high_quality")).toBe("weighted");
    expect(
      recordScoreKind(
        { score: 0.03, fusion: both },
        { ...hybrid, reranking_mode: "rrf" },
        "high_quality",
      ),
    ).toBe("rrf");
    const llm = { ...hybrid, reranking_mode: "llm" as const };
    expect(
      recordScoreKind({ score: 0.9, fusion: both, fusion_score: 0.85 }, llm, "high_quality"),
    ).toBe("llm");
    expect(
      recordScoreKind({ score: 0.85, fusion: both, fusion_score: 0.85 }, llm, "high_quality", [
        { code: "rerank_failed" },
      ]),
    ).toBe("weighted");
  });

  it("전문 검색, 경제 KB, 질의 임베딩 실패, 벡터 없이 전문으로 돈 결과는 bm25 기준이다", () => {
    const fullText = { ...hybrid, search_method: "full_text_search" as const };
    expect(recordScoreKind({ score: 0.9, fusion: keyword }, fullText, "high_quality")).toBe(
      "keyword",
    );
    expect(recordScoreKind({ score: 0.9, fusion: keyword }, hybrid, "economy")).toBe("keyword");
    expect(
      recordScoreKind({ score: 0.9, fusion: keyword }, hybrid, "high_quality", [
        { code: "vector_unavailable" },
      ]),
    ).toBe("keyword");
    // 벡터 색인이 아직 없으면 벡터 전용 검색도 전문 검색으로 돈다. 점수가 키워드 점수 그대로다.
    expect(
      recordScoreKind(
        { score: 0.9, fusion: keyword },
        { ...hybrid, search_method: "semantic_search" },
        "high_quality",
      ),
    ).toBe("keyword");
    // 하이브리드에서 전문 후보로만 잡힌 결과는 키워드 몫(0.3배)만 받으므로 결합 점수다.
    expect(recordScoreKind({ score: 0.27, fusion: keyword }, hybrid, "high_quality")).toBe(
      "weighted",
    );
  });
});
