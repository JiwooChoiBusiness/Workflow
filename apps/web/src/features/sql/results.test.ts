import { describe, expect, it } from "vitest";
import { csvFilename, parseToolResult, resultSetOf, toCsv } from "./results";

describe("SQL 결과", () => {
  it("CSV는 BOM과 CRLF를 붙이고 쉼표·따옴표·줄바꿈이 든 칸만 감싼다", () => {
    const csv = toCsv(
      ["region", "memo", "n"],
      [
        { region: "서울", memo: '말하길 "좋다", 끝', n: 2 },
        { region: null, memo: "두\n줄", n: 1.5 },
      ],
    );
    expect(csv).toBe('\uFEFFregion,memo,n\r\n서울,"말하길 ""좋다"", 끝",2\r\n,"두\n줄",1.5\r\n');
  });

  it("수식으로 읽힐 글 칸은 앞에 '를 붙이고 숫자와 숫자 모양 글은 그대로 둔다", () => {
    const values = ['=HYPERLINK("http://x")', "+1+2", "-2+3", "@SUM(A1)", "\tx", "\r=1"];
    const csv = toCsv(
      ["v"],
      [...values, -5, "-12.50", "+1e3", "-inf", "2026-11-04", true, "a=b"].map((v) => ({ v })),
    );
    expect(csv.slice(csv.indexOf("\r\n") + 2)).toBe(
      [
        `"'=HYPERLINK(""http://x"")"`,
        "'+1+2",
        "'-2+3",
        "'@SUM(A1)",
        "'\tx",
        `"'\r=1"`,
        "-5",
        "-12.50",
        "+1e3",
        "-inf",
        "2026-11-04",
        "true",
        "a=b",
        "",
      ].join("\r\n"),
    );
  });

  it("머리글(모델이 쓴 SQL 별칭)도 같은 규칙으로 중화하고, 숫자 모양 열 이름도 글로 본다", () => {
    const columns = ["=1+1", "-1", "@who", "\tname", "region", "a-b"];
    const csv = toCsv(columns, [Object.fromEntries(columns.map((name) => [name, 1]))]);
    const header = csv.slice(1, csv.indexOf("\r\n"));
    expect(header).toBe(["'=1+1", "'-1", "'@who", "'\tname", "region", "a-b"].join(","));
    expect(csv.split("\r\n")[1]).toBe("1,1,1,1,1,1");
  });

  it("내려받기 이름은 파일에 못 쓰는 글자를 바꾸고 시각을 붙인다", () => {
    expect(csvFilename("매출 / 2026", new Date("2026-11-04T01:02:03Z"))).toBe(
      "매출_2026-20261104010203.csv",
    );
  });

  it("노드 출력의 columns·rows를 읽고 미리보기에서 잘린 값은 건너뛴다", () => {
    expect(resultSetOf({ columns: ["a", 1], rows: [{ a: 1 }, "…"] })).toEqual({
      columns: ["a"],
      rows: [{ a: 1 }],
    });
    expect(resultSetOf({ error: "x" })).toBeNull();
  });

  it("query_sql 결과 글의 표·요약 줄·CSV 경로를 읽는다(\\| 이스케이프와 NULL 포함)", () => {
    const output = [
      '<document name="질의 결과" source="쇼핑몰">',
      "| region | memo |",
      "| --- | --- |",
      "| 서울 | a \\| b |",
      "| 부산 | NULL |",
      "</document>",
      "결과가 200행을 넘어 잘렸다. 처음 2행만 싣는다.",
      "실행한 SQL: SELECT region, memo FROM t LIMIT 201",
      "결과가 잘려 행 상한까지 받은 행만 작업공간 sql/call_1.csv에 CSV로 저장했다.",
    ].join("\n");
    expect(parseToolResult(output)).toEqual({
      table: {
        columns: ["region", "memo"],
        rows: [
          { region: "서울", memo: "a | b" },
          { region: "부산", memo: null },
        ],
      },
      summary: "결과가 200행을 넘어 잘렸다. 처음 2행만 싣는다.",
      rowCount: 200,
      shown: 2,
      truncated: true,
      executed: "SELECT region, memo FROM t LIMIT 201",
      csvPath: "sql/call_1.csv",
    });
  });

  it("결과 행이 없으면 표 없이 요약만 읽는다", () => {
    expect(parseToolResult("결과 행이 없다.\n실행한 SQL: SELECT 1 LIMIT 201")).toEqual({
      table: null,
      summary: "결과 행이 없다.",
      rowCount: 0,
      shown: 0,
      truncated: false,
      executed: "SELECT 1 LIMIT 201",
      csvPath: null,
    });
  });

  it("요약 줄에서 받은 행 수를 읽고, 여러 줄 SQL은 CSV 안내 줄 앞까지 통째로 읽는다", () => {
    const rows = Array.from({ length: 50 }, (_, index) => `| ${index + 1} |`);
    const sql = "SELECT n\nFROM t\nWHERE memo <> '실행한 SQL: 아님'\nLIMIT 201";
    const parsed = parseToolResult(
      [
        '<document name="질의 결과" source="쇼핑몰">',
        "| n |",
        "| --- |",
        ...rows,
        "</document>",
        "120행 중 처음 50행만 싣는다.",
        `실행한 SQL: ${sql}`,
      ].join("\n"),
    );
    expect(parsed).toMatchObject({ rowCount: 120, shown: 50, truncated: false, csvPath: null });
    expect(parsed.table?.rows).toHaveLength(50);
    expect(parsed.executed).toBe(sql);

    // 옛 서버가 남긴 기록의 끝줄("전체 결과는 …")도 꼬리로 읽는다.
    const cut = parseToolResult(
      [
        "결과가 1000행을 넘어 잘렸다. 처음 0행만 싣는다.",
        "실행한 SQL: SELECT *",
        "FROM t",
        "LIMIT 1001",
        "전체 결과는 작업공간 sql/call_2.csv에 CSV로 저장했다.",
      ].join("\n"),
    );
    expect(cut).toMatchObject({
      rowCount: 1000,
      shown: 0,
      truncated: true,
      executed: "SELECT *\nFROM t\nLIMIT 1001",
      csvPath: "sql/call_2.csv",
    });
    expect(parseToolResult("3행.\n실행한 SQL: SELECT 1")).toMatchObject({ rowCount: 3, shown: 3 });
  });
});
