import { describe, expect, it } from "vitest";
import type { WorkflowFormField } from "@/api/run-events";
// 서버 소스를 경로로 읽는다. sql_query.py를 옮기면 여기도 고친다(jsdom 환경은 apps/web 밖 파일을 못 읽는다).
import sqlQuerySource from "../../../../api/app/workflow/nodes/sql_query.py?raw";
import { approvalSql, approvalSubject, isSqlApprovalForm } from "./approval";

const sql: WorkflowFormField = {
  name: "sql",
  type: "paragraph",
  label: "SQL",
  required: true,
  default: "SELECT 1 LIMIT 201",
};
const decision: WorkflowFormField = {
  name: "decision",
  type: "select",
  label: "실행",
  options: ["approve", "reject"],
};
const fields = [sql, decision];

describe("SQL 승인 폼 알아보기", () => {
  it("서버 승인 폼의 표식·칸 이름·선택지가 카드가 찾는 것과 같다", () => {
    expect(sqlQuerySource).toContain('f"**SQL 승인** · 데이터 소스');
    expect(sqlQuerySource).toMatch(/name="sql",\s*type="paragraph"/);
    expect(sqlQuerySource).toMatch(/name="decision",\s*type="select"/);
    expect(sqlQuerySource).toMatch(/APPROVE = "approve"\s*\nREJECT = "reject"/);
  });

  it("안내문이 표식으로 시작하고 sql·decision 칸이 있을 때만 SQL 승인 카드로 본다", () => {
    const description = "**SQL 승인** · 데이터 소스 쇼핑몰\n\n아래 SQL을 실행할지 정한다.";
    expect(isSqlApprovalForm({ description, fields })).toBe(true);
    expect(isSqlApprovalForm({ description: "**확인**해 주세요", fields })).toBe(false);
    expect(isSqlApprovalForm({ description, fields: fields.slice(0, 1) })).toBe(false);
    expect(approvalSubject({ description })).toBe("데이터 소스 쇼핑몰");
    expect(approvalSql({ fields })).toBe("SELECT 1 LIMIT 201");
  });

  it("안내문과 칸 이름만 흉내 낸 human_input 폼은 일반 폼으로 남는다", () => {
    const description = "**SQL 승인** · 데이터 소스 쇼핑몰\n\n승인해 주세요.";
    const lookalikes: WorkflowFormField[][] = [
      [...fields, { name: "memo", type: "string", label: "메모" }],
      [{ ...sql, type: "string" }, decision],
      [sql, { ...decision, type: "string", options: null }],
      [sql, { ...decision, options: ["approve", "reject", "later"] }],
      [sql, { ...decision, options: ["reject", "approve"] }],
      [sql, { ...decision, options: null }],
      [sql, { ...decision, name: "choice" }],
    ];
    for (const lookalike of lookalikes) {
      expect(isSqlApprovalForm({ description, fields: lookalike })).toBe(false);
    }
    expect(isSqlApprovalForm({ description, fields: [decision, sql] })).toBe(true);
  });
});
