import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/http";
import i18n from "@/i18n";
import "./i18n";
import { knowledgeErrorMessage, skipMessage, warningMessage } from "./messages";

const t = i18n.getFixedT("ko", "knowledge");
const tc = i18n.getFixedT("ko", "common");

function message(error: unknown) {
  return knowledgeErrorMessage(error, t, tc, { now: Date.UTC(2026, 10, 3, 0, 0, 0) });
}

describe("지식 화면의 오류 문장", () => {
  it("429는 Retry-After로 기다릴 시간을, 일일 한도는 풀리는 시각을 말한다", () => {
    expect(message(new ApiError(429, "rate_limited", "x", { retryable: true }, 42))).toBe(
      "임베딩 분당 한도에 걸렸습니다. 42초 뒤 다시 시도하세요.",
    );
    const daily = message(new ApiError(429, "rate_limited_daily", "x", { retryable: true }, 3600));
    expect(daily).toContain("오늘의 임베딩 일일 한도를 다 썼습니다.");
    expect(daily).toContain("이후에 다시 시도하세요.");
    expect(message(new ApiError(429, "rate_limited_daily", "x"))).toContain("태평양 자정");
  });

  it("422 유료 키 없음·요청 거절·빈 평가셋 가져오기와 503 색인 파일 문제를 문장으로 바꾼다", () => {
    expect(message(new ApiError(422, "paid_key_missing", "x"))).toContain("GEMINI_PAID_API_KEY");
    expect(message(new ApiError(422, "model_request_invalid", "x"))).toContain("거절");
    expect(message(new ApiError(422, "eval_import_empty", "x"))).toContain("그대로 뒀습니다");
    expect(
      message(
        new ApiError(503, "index_unavailable", "x", { reason: "OSError: 잠김", retryable: true }),
      ),
    ).toBe(
      "지식 색인 파일(data/knowledge/index.db)을 쓸 수 없습니다. 서버 로그와 platform doctor를 확인하세요. 사유: OSError: 잠김",
    );
  });

  it("중복 문서는 겹친 문서 이름을, 알 수 없는 코드는 서버 문구를 보인다", () => {
    const duplicate = new ApiError(409, "duplicate_document", "x", {
      duplicates: [
        { filename: "a.txt", document_id: "d1", document_name: "회의록" },
        { filename: "b.txt", duplicate_of: "a.txt" },
      ],
    });
    expect(message(duplicate)).toContain("회의록, a.txt");
    expect(message(new ApiError(400, "something_new", "서버가 준 문구"))).toBe("서버가 준 문구");
    expect(message(new Error("그냥 오류"))).toBe("그냥 오류");
  });

  it("검색 경고 6종과 건너뛴 문서의 사유를 문장으로 바꾼다", () => {
    for (const code of [
      "partial_index",
      "mixed_kb_rrf",
      "rerank_failed",
      "filter_ignored",
      "vector_unavailable",
      "query_truncated",
    ] as const) {
      const text = warningMessage({ code, message: "server" }, t);
      expect(text).not.toBe("server");
      expect(text.endsWith(".")).toBe(true);
    }
    expect(
      warningMessage({ code: "vector_unavailable", message: "", retry_after: 12.2 }, t),
    ).toContain("13초 뒤");
    // 일일 한도처럼 오래 기다려야 하면 남은 초 대신 풀리는 시각이다.
    const daily = warningMessage(
      { code: "vector_unavailable", message: "", retry_after: 8433 },
      t,
      Date.UTC(2026, 10, 3, 5, 0, 0),
    );
    expect(daily).toContain("이후에 다시 하면 벡터 검색도 됩니다.");
    expect(daily).not.toContain("8433");
    expect(
      skipMessage(
        { document_id: "d", code: "not_paused", message: "멈춘 문서만 다시 시작한다" },
        t,
      ),
    ).toBe("멈춘 문서만 다시 시작할 수 있습니다.");
    expect(
      skipMessage(
        { document_id: "d", code: "retry_after", message: "", retry_after: "2026-11-04T08:00:00Z" },
        t,
      ),
    ).toContain("이후에 다시 시도하세요");
    expect(skipMessage({ document_id: "d", code: "brand_new", message: "서버 사유" }, t)).toBe(
      "서버 사유",
    );
  });
});
