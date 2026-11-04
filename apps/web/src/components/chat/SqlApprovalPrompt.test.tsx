// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import type { WorkflowFormData } from "@/api/run-events";
import type { WfFileOut } from "@/api/types";
import { FormPrompt } from "./FormPrompt";

// CodeMirror는 jsdom에서 그리지 않는다. 같은 값·이름을 쓰는 글 칸으로 바꾼다.
vi.mock("@/components/ui/lazy-code-editor", () => ({
  CodeEditor: ({
    value,
    onChange,
    ariaLabel,
    readOnly,
  }: {
    value: string;
    onChange?: (value: string) => void;
    ariaLabel: string;
    readOnly?: boolean;
  }) => (
    <textarea
      aria-label={ariaLabel}
      value={value}
      readOnly={readOnly}
      onChange={(event) => onChange?.(event.target.value)}
    />
  ),
}));

afterEach(cleanup);

const SQL = "SELECT region, COUNT(*) AS n FROM customers GROUP BY region LIMIT 201";

// 서버 sql_query.approval_text·approval_fields가 만드는 폼과 같은 모양(서버 소스와의 대조는
// features/sql/approval.test.ts가 한다. jsdom 시험은 apps/web 밖 파일을 읽지 못한다).
const sqlForm: WorkflowFormData = {
  form_id: "form_sql",
  description:
    "**SQL 승인** · 데이터 소스 쇼핑몰\n\n아래 SQL을 실행할지 정한다. 고쳐서 승인하면 다시 검증한 뒤 " +
    "실행하고, 거절하면 이 노드가 sql_rejected로 실패한다.",
  fields: [
    {
      name: "sql",
      type: "paragraph",
      label: "SQL",
      required: true,
      default: SQL,
      max_length: 32768,
    },
    {
      name: "decision",
      type: "select",
      label: "실행",
      required: true,
      default: "approve",
      options: ["approve", "reject"],
    },
  ],
  created_at: "2026-11-04T00:00:00Z",
};

function noUpload(): Promise<WfFileOut> {
  return Promise.reject(new Error("upload_unavailable"));
}

async function setup(onSubmit = vi.fn(async (): Promise<true | unknown> => true)) {
  render(<FormPrompt form={sqlForm} nodeTitle="SQL 실행" upload={noUpload} onSubmit={onSubmit} />);
  await screen.findByRole("region", { name: "SQL 승인 요청" });
  return { onSubmit, user: userEvent.setup() };
}

describe("SQL 승인 카드", () => {
  it("안내문이 SQL 승인이면 필드 폼 대신 SQL 편집기와 승인·거절을 그린다", async () => {
    await setup();
    expect(
      screen.getByRole("heading", { name: "SQL 실행 노드가 SQL 실행 승인을 기다립니다" }),
    ).toBeTruthy();
    expect(screen.getByText("데이터 소스 쇼핑몰")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "실행할 SQL" }) as HTMLTextAreaElement).value).toBe(
      SQL,
    );
    expect(screen.queryByRole("button", { name: "보내기" })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("고친 SQL로 승인하면 {sql, decision: approve}를 보내고 재검사 안내를 보인다", async () => {
    const { onSubmit, user } = await setup();
    const box = screen.getByRole("textbox", { name: "실행할 SQL" });
    await user.clear(box);
    await user.type(box, "SELECT region FROM customers");
    expect(screen.getByText("고침")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "승인하고 실행" }));
    expect(onSubmit).toHaveBeenCalledWith({
      sql: "SELECT region FROM customers",
      decision: "approve",
    });
    expect(screen.getByRole("status").textContent).toMatch(/다시 검사한 뒤 실행하고/);
    expect(screen.getByRole("button", { name: "승인하고 실행" }).hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("거절은 decision reject를 보내고, 원래 SQL로 되돌릴 수 있다", async () => {
    const { onSubmit, user } = await setup();
    const box = screen.getByRole("textbox", { name: "실행할 SQL" });
    await user.type(box, " WHERE 1=1");
    await user.click(screen.getByRole("button", { name: "원래 SQL로" }));
    expect((box as HTMLTextAreaElement).value).toBe(SQL);
    await user.click(screen.getByRole("button", { name: "거절" }));
    expect(onSubmit).toHaveBeenCalledWith({ sql: SQL, decision: "reject" });
    expect(screen.getByRole("status").textContent).toBe(
      "거절했습니다. 이 노드는 실행하지 않고 실패로 끝납니다.",
    );
  });

  it("SQL을 비우고 승인하면 보내지 않는다", async () => {
    const { onSubmit, user } = await setup();
    await user.clear(screen.getByRole("textbox", { name: "실행할 SQL" }));
    await user.click(screen.getByRole("button", { name: "승인하고 실행" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toBe("실행할 SQL을 쓰세요.");
  });

  it("이미 닫힌 폼(409)과 칸 오류(422)를 카드 안에 알린다", async () => {
    const closed = vi.fn(
      async (): Promise<true | unknown> =>
        new ApiError(409, "run_not_waiting", "런이 기다리지 않는다"),
    );
    const { user } = await setup(closed);
    await user.click(screen.getByRole("button", { name: "승인하고 실행" }));
    expect(
      (await screen.findAllByText(/이 런은 더 이상 입력을 기다리지 않습니다/)).length,
    ).toBeGreaterThan(0);
    cleanup();
    const invalid = vi.fn(
      async (): Promise<true | unknown> =>
        new ApiError(422, "invalid_inputs", "폼 값이 올바르지 않다", {
          sql: "32768자 이하여야 한다",
        }),
    );
    const second = await setup(invalid);
    await second.user.click(screen.getByRole("button", { name: "승인하고 실행" }));
    expect(screen.getByRole("alert").textContent).toBe("32768자 이하여야 한다");
  });
});
