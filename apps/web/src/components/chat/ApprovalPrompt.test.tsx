// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WaitingInputData } from "@/api/run-events";
import { ApprovalPrompt } from "./ApprovalPrompt";

afterEach(cleanup);

const command = [
  "$r = Invoke-WebRequest -Uri https://pypi.org -UseBasicParsing",
  '"상태 코드: $($r.StatusCode)" | Out-File outputs\\status.txt -Encoding utf8',
  "Remove-Item -Recurse -Force outputs\\old",
].join("\n");

const shell: WaitingInputData = {
  interaction_id: "int_01",
  kind: "approval",
  tool_call_id: "call_02",
  tool_name: "shell",
  input: { command, timeout_sec: 60 },
  cwd: "C:\\wf-data\\workspaces\\s1",
  timeout_sec: 60,
};

function setup(interaction: WaitingInputData = shell, busy = false) {
  const onApprove = vi.fn();
  const onReject = vi.fn();
  render(
    <ApprovalPrompt
      interaction={interaction}
      onApprove={onApprove}
      onReject={onReject}
      busy={busy}
    />,
  );
  return { onApprove, onReject, user: userEvent.setup() };
}

describe("ApprovalPrompt", () => {
  it("명령 전문, 작업 디렉터리, 타임아웃을 자르지 않고 보여 준다", () => {
    setup();
    expect(screen.getByRole("heading", { name: "실행 승인 요청" })).toBeTruthy();
    const pre = screen.getByText(
      (_, element) => element?.tagName === "PRE" && element.textContent === command,
    );
    expect(pre).toBeTruthy();
    expect(screen.getByText("C:\\wf-data\\workspaces\\s1")).toBeTruthy();
    expect(screen.getByText("60초")).toBeTruthy();
    expect(screen.getByText("shell")).toBeTruthy();
  });

  it("승인하면 '이 세션에서는 묻지 않기' 선택을 함께 넘긴다", async () => {
    const { onApprove, onReject, user } = setup();
    await user.click(screen.getByRole("button", { name: "승인" }));
    expect(onApprove).toHaveBeenLastCalledWith({ sessionAutoApprove: false });

    await user.click(screen.getByRole("checkbox", { name: "이 세션에서는 묻지 않기" }));
    await user.click(screen.getByRole("button", { name: "승인" }));
    expect(onApprove).toHaveBeenLastCalledWith({ sessionAutoApprove: true });
    expect(onReject).not.toHaveBeenCalled();
  });

  it("거절하면 적은 사유를 넘긴다", async () => {
    const { onApprove, onReject, user } = setup();
    await user.type(
      screen.getByRole("textbox", { name: "거절 사유 (선택)" }),
      "  삭제는 하지 마  ",
    );
    await user.click(screen.getByRole("button", { name: "거절" }));
    expect(onReject).toHaveBeenCalledWith("삭제는 하지 마");
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("자동 포커스와 키보드 단축키가 없어 Enter로 승인되지 않는다", async () => {
    const { onApprove, onReject, user } = setup();
    expect(document.activeElement).toBe(document.body);
    await user.type(screen.getByRole("textbox", { name: "거절 사유 (선택)" }), "이유{Enter}");
    await user.keyboard("{Enter}");
    expect(onApprove).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
  });

  it("응답을 보내는 중에는 버튼과 입력을 막는다", () => {
    setup(shell, true);
    expect((screen.getByRole("button", { name: "승인" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "거절" }) as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByRole("textbox", { name: "거절 사유 (선택)" }) as HTMLTextAreaElement).disabled,
    ).toBe(true);
  });

  it("http_request는 민감한 헤더 값을 가리고 본문을 보여 준다", () => {
    setup({
      ...shell,
      tool_name: "http_request",
      input: {
        method: "POST",
        url: "https://api.example.com/items",
        headers: { Authorization: "Bearer secret-token", "X-Trace": "abc" },
        body: '{"name":"새 항목"}',
      },
    });
    expect(screen.getByText("POST https://api.example.com/items")).toBeTruthy();
    const headers = screen.getByText(
      (_, element) =>
        element?.tagName === "PRE" && element.textContent === "Authorization: ***\nX-Trace: abc",
    );
    expect(headers).toBeTruthy();
    expect(screen.queryByText(/secret-token/)).toBeNull();
    expect(screen.getByText('{"name":"새 항목"}')).toBeTruthy();
  });

  it("install_packages는 설치할 패키지 목록을 보여 준다", () => {
    setup({
      ...shell,
      tool_name: "install_packages",
      input: { packages: ["requests", "pandas==2.*"] },
    });
    expect(screen.getByText("설치할 패키지")).toBeTruthy();
    expect(screen.getByText("requests")).toBeTruthy();
    expect(screen.getByText("pandas==2.*")).toBeTruthy();
  });
});
