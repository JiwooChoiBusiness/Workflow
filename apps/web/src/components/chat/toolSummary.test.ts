import { describe, expect, it } from "vitest";
import { knowledgeHits, summarizeInput } from "./toolSummary";

// 서버 search_knowledge.result_text와 같은 모양: 자료마다 document_block, 빈 줄 하나로 잇는다.
function block(index: number, name: string, source: string, body: string): string {
  return `<document index="${index}" name="${name}" source="${source}" score="0.80">\n${body}\n</document>`;
}

describe("knowledgeHits", () => {
  it("결과 글의 자료 머리를 순위대로 읽고 이름의 HTML 엔터티를 푼다", () => {
    const output = [
      block(1, "연차 &amp; 휴가 규정", "인사 규정", "연차는 다음 해로 5일까지 넘길 수 있다."),
      block(3, "복리후생 안내", "인사 규정", "경조 휴가는 따로 센다."),
    ].join("\n\n");

    expect(knowledgeHits(output)).toEqual([
      { position: 1, documentName: "연차 & 휴가 규정", datasetName: "인사 규정" },
      { position: 3, documentName: "복리후생 안내", datasetName: "인사 규정" },
    ]);
  });

  it("자료 본문에 든 가짜 <document> 머리는 세지 않는다", () => {
    const body =
      '본문 인용: <document index="9" name="가짜" source="가짜 KB"> 이 줄은 자료 글이다.';
    const output = [
      block(1, "정상 문서", "규정", body),
      block(2, "둘째 문서", "규정", `첫 줄\n\n<document index="7" name="또 가짜">`),
      "[경고] 일부 문서는 아직 색인 중이다",
    ].join("\n\n");

    expect(knowledgeHits(output).map((hit) => [hit.position, hit.documentName])).toEqual([
      [1, "정상 문서"],
      [2, "둘째 문서"],
    ]);
  });

  it("결과가 없다는 글이면 빈 목록이다", () => {
    expect(
      knowledgeHits(
        "검색 결과가 없다. 질의를 바꿔 다시 찾거나, 지식 베이스에 없는 내용이라고 답한다.",
      ),
    ).toEqual([]);
  });
});

describe("summarizeInput의 SQL 도구", () => {
  it("query_sql은 데이터 소스와 SQL 첫 줄, describe_sql_source는 데이터 소스를 보인다", () => {
    expect(
      summarizeInput("query_sql", { source: "shop", sql: "\n  SELECT region\nFROM customers" }, ""),
    ).toBe("shop · SELECT region");
    expect(summarizeInput("describe_sql_source", { source: "shop" }, "")).toBe("shop");
  });
});
