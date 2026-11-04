/*
 * sql_query 노드의 승인 폼(설계 10.6). human_input과 같은 폼 대기이고 폼은 {sql: paragraph, decision: select}다.
 * 안내문이 "**SQL 승인**"으로 시작하고 칸 모양이 같으면 SQL 승인 카드로 그린다(apps/api/app/workflow/nodes/
 * sql_query.py approval_text). 답은 /input에 {kind: "form", values: {sql, decision}}으로 보낸다.
 */
import type { WorkflowFormData } from "@/api/run-events";

export const SQL_APPROVAL_MARK = "**SQL 승인**";
export const APPROVE = "approve";
export const REJECT = "reject";

export type SqlDecision = typeof APPROVE | typeof REJECT;

// human_input 폼도 같은 안내문과 칸 이름을 쓸 수 있어, 칸 수·종류·선택지까지 서버 승인 폼과 같을 때만 카드로 본다.
export function isSqlApprovalForm(form: Pick<WorkflowFormData, "description" | "fields">): boolean {
  if (!(form.description ?? "").startsWith(SQL_APPROVAL_MARK) || form.fields.length !== 2) {
    return false;
  }
  const sql = form.fields.find((field) => field.name === "sql");
  const decision = form.fields.find((field) => field.name === "decision");
  const options = decision?.options ?? [];
  return (
    sql?.type === "paragraph" &&
    decision?.type === "select" &&
    options.length === 2 &&
    options[0] === APPROVE &&
    options[1] === REJECT
  );
}

// 안내문 첫 줄의 표식 뒤("데이터 소스 <이름>"). 나머지 안내는 카드가 자기 문장으로 보인다.
export function approvalSubject(form: Pick<WorkflowFormData, "description">): string {
  const first = (form.description ?? "").split(/\r?\n/, 1)[0] ?? "";
  return first
    .replace(SQL_APPROVAL_MARK, "")
    .replace(/^\s*·\s*/, "")
    .trim();
}

// 폼에 채워 온 검증된 SQL(고치기 전 값)
export function approvalSql(form: Pick<WorkflowFormData, "fields">): string {
  const field = form.fields.find((item) => item.name === "sql");
  return typeof field?.default === "string" ? field.default : "";
}

export function approvalValues(sql: string, decision: SqlDecision): Record<string, unknown> {
  return { sql, decision };
}
