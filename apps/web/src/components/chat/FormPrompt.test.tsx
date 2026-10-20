// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/http";
import type { WorkflowFormData } from "@/api/run-events";
import type { WfFileOut } from "@/api/types";
import { secondNodesContract } from "@/test/secondNodesContract";
import { FormPrompt } from "./FormPrompt";

afterEach(cleanup);

const form: WorkflowFormData = {
  form_id: "form_1",
  description: "**확인**해 주세요",
  fields: [
    { name: "who", type: "string", label: "이름", required: true },
    { name: "count", type: "number", label: "개수" },
    { name: "note", type: "paragraph", label: "메모" },
    { name: "ok", type: "boolean", label: "괜찮은가", default: true },
    { name: "proof", type: "file", label: "증빙" },
  ],
  created_at: "2026-10-15T00:00:00Z",
};

const uploaded: WfFileOut = {
  id: "f1",
  kind: "upload",
  workflow_id: "w1",
  wf_run_id: null,
  filename: "a.txt",
  mime: "text/plain",
  size_bytes: 3,
  sha256: "x",
  created_at: "2026-10-15T00:00:00Z",
};

function setup(onSubmit = vi.fn(async (): Promise<true | unknown> => true)) {
  const upload = vi.fn(async () => uploaded);
  render(<FormPrompt form={form} nodeTitle="확인" upload={upload} onSubmit={onSubmit} />);
  return { onSubmit, upload, user: userEvent.setup() };
}

describe("FormPrompt", () => {
  it("안내문(마크다운)과 필드를 그리고, 필수 칸이 비었으면 보내지 않는다", async () => {
    const { onSubmit, user } = setup();
    expect(screen.getByRole("heading", { name: "확인 노드가 입력을 기다립니다" })).toBeTruthy();
    expect(screen.getByText("확인").tagName).toBe("STRONG");
    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(screen.getByText("필수 입력입니다.")).toBeTruthy();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("값을 필드 이름으로 보내고(파일 칸은 올린 id) 받으면 이어서 돈다고 알린다", async () => {
    const { onSubmit, upload, user } = setup();
    await user.type(screen.getByRole("textbox", { name: "이름" }), "홍길동");
    await user.type(screen.getByLabelText("개수"), "3");
    await user.type(screen.getByLabelText("메모"), "좋음");
    await user.upload(screen.getByLabelText("증빙"), new File(["abc"], "a.txt"));
    expect(upload).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(onSubmit).toHaveBeenCalledWith({
      who: "홍길동",
      count: 3,
      note: "좋음",
      ok: true,
      proof: "f1",
    });
    expect(screen.getByRole("status").textContent).toBe("보냈습니다. 같은 런이 이어서 돕니다.");
    expect((screen.getByRole("button", { name: "보내기" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("서버가 칸을 거절하면 그 칸 아래에, 폼이 닫혔으면 이유를 보인다", async () => {
    const { invalid, again } = secondNodesContract;
    const onSubmit = vi
      .fn<() => Promise<true | unknown>>()
      .mockResolvedValueOnce(
        new ApiError(422, invalid.body.error.code, invalid.body.error.message, {
          who: "필수 입력이다",
        }),
      )
      .mockResolvedValueOnce(
        new ApiError(409, "input_timeout", "응답 대기 시간이 지나 폼이 닫혔다"),
      )
      .mockResolvedValueOnce(new ApiError(409, again.body.error.code, again.body.error.message));
    const { user } = setup(onSubmit);
    await user.type(screen.getByRole("textbox", { name: "이름" }), "x");
    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(screen.getByText("필수 입력이다")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(screen.getByText(/응답을 기다리는 시간이 지나 폼이 닫혔습니다/)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "보내기" }));
    expect(screen.getByText(/이 런은 더 이상 입력을 기다리지 않습니다/)).toBeTruthy();
  });
});
