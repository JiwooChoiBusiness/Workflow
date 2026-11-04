// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { appendOutput, createRunStore, newToolCall, type ChangedFile } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { ToolCallCard } from "./ToolCallCard";

afterEach(cleanup);

// apps/api/app/agent/tools/query_sql.py가 내는 결과 글과 같은 모양.
const OUTPUT = [
  '<document name="질의 결과" source="쇼핑몰">',
  "| region | n |",
  "| --- | --- |",
  "| 부산 | 1 |",
  "| 서울 | 2 |",
  "</document>",
  "결과가 2행을 넘어 잘렸다. 처음 2행만 싣는다.",
  "실행한 SQL: SELECT region, COUNT(*) AS n FROM customers GROUP BY region LIMIT 3",
  "전체 결과는 작업공간 sql/call_1.csv에 CSV로 저장했다.",
].join("\n");

function show(files: ChangedFile[], output = OUTPUT, restored = false) {
  const tool = {
    ...appendOutput(newToolCall("call_1", "r1", 1, "query_sql"), "stdout", output),
    input: { source: "shop", sql: "SELECT region, COUNT(*) AS n FROM customers GROUP BY region" },
    phase: "done" as const,
    result: "ok" as const,
    // 메시지 기록에서 다시 그린 카드에는 tool_call.completed.sql이 없다.
    ...(restored
      ? {}
      : {
          sql: {
            sql_executed: "SELECT region, COUNT(*) AS n FROM customers GROUP BY region LIMIT 3",
            row_count: 2,
            truncated: true,
          },
        }),
  };
  const store = createRunStore({ initial: { toolCalls: { call_1: tool }, files } });
  render(
    <RunStoreProvider store={store}>
      <ToolCallCard toolCallId="call_1" fileHref={(id) => `/files/${id}`} />
    </RunStoreProvider>,
  );
}

describe("query_sql 도구 카드", () => {
  it("행 수·잘림·실행한 SQL과 결과 글의 표를 보이고, 런이 끝나기 전에는 CSV 경로만 보인다", () => {
    show([]);
    expect(
      screen.getByText("shop · SELECT region, COUNT(*) AS n FROM customers GROUP BY region"),
    ).toBeTruthy();
    expect(screen.getByText("2행")).toBeTruthy();
    expect(screen.getByText("잘림 · 전체는 CSV")).toBeTruthy();
    expect(screen.getByLabelText("실행한 SQL").textContent).toContain("LIMIT 3");
    const table = screen.getByRole("table", { name: "결과 표" });
    expect(within(table).getByRole("cell", { name: "서울" })).toBeTruthy();
    expect(screen.getByText("sql/call_1.csv · 런이 끝나면 파일 목록에서 받습니다")).toBeTruthy();
  });

  it("file.changed로 받은 잘린 결과 파일을 내려받기 링크로 잇는다", () => {
    show([
      {
        path: "sql/call_1.csv",
        size: 120,
        sessionFileId: "sf1",
        downloadable: true,
        reason: null,
        runId: "r1",
      },
    ]);
    expect(screen.getByRole("link", { name: /잘린 결과 CSV/ }).getAttribute("href")).toBe(
      "/files/sf1",
    );
  });

  it("기록에서 다시 그린 카드는 결과 글의 요약 줄에서 행 수를, 실행한 SQL은 여러 줄 통째로 읽는다", () => {
    const sql = "SELECT region, n\nFROM customers\nORDER BY n DESC\nLIMIT 201";
    const output = [
      '<document name="질의 결과" source="쇼핑몰">',
      "| region | n |",
      "| --- | --- |",
      ...Array.from({ length: 50 }, (_, index) => `| r${index} | ${index} |`),
      "</document>",
      "120행 중 처음 50행만 싣는다.",
      `실행한 SQL: ${sql}`,
    ].join("\n");
    show([], output, true);
    expect(screen.getByText("120행")).toBeTruthy();
    expect(screen.getByText("앞 50행")).toBeTruthy();
    expect(screen.queryByText("잘림 · 전체는 CSV")).toBeNull();
    expect(screen.getByLabelText("실행한 SQL").textContent).toBe(sql);
  });
});
