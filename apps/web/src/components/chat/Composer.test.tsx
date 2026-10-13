// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer, MAX_ATTACHMENT_BYTES } from "./Composer";

afterEach(cleanup);

function setup(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn<(text: string, files: File[]) => boolean | void>(() => true);
  const onStop = vi.fn();
  render(<Composer onSend={onSend} onStop={onStop} running={false} attachments {...props} />);
  return { onSend, onStop, user: userEvent.setup() };
}

function textbox() {
  return screen.getByRole("textbox", { name: "메시지" }) as HTMLTextAreaElement;
}

function button(name: string) {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

function chips() {
  return screen.queryByRole("list", { name: "첨부할 파일" })?.textContent ?? null;
}

function csv(name = "입력.csv") {
  return new File(["a,b\n1,2\n"], name, { type: "text/csv" });
}

describe("Composer", () => {
  it("고른 파일을 칩으로 보여 주고 글과 함께 넘긴다", async () => {
    const { onSend, user } = setup();
    await user.upload(screen.getByLabelText("첨부할 파일 고르기"), csv());
    expect(chips()).toContain("입력.csv");
    await user.type(textbox(), "파일 봐 줘{Enter}");
    expect(onSend).toHaveBeenCalledWith("파일 봐 줘", [
      expect.objectContaining({ name: "입력.csv" }),
    ]);
    expect(chips()).toBeNull();
    expect(textbox().value).toBe("");
  });

  it("서버가 받지 않으면(false) 글과 파일을 되돌려 놓는다", async () => {
    const { user } = setup({ onSend: () => Promise.resolve(false) });
    await user.upload(screen.getByLabelText("첨부할 파일 고르기"), csv());
    await user.type(textbox(), "겹친 메시지{Enter}");
    await waitFor(() => expect(textbox().value).toBe("겹친 메시지"));
    expect(chips()).toContain("입력.csv");
  });

  it("100MB를 넘는 파일은 빼고 알린다", async () => {
    const { user } = setup();
    const big = csv("큰파일.bin");
    Object.defineProperty(big, "size", { value: MAX_ATTACHMENT_BYTES + 1 });
    await user.upload(screen.getByLabelText("첨부할 파일 고르기"), [big, csv()]);
    expect(screen.getByText("'큰파일.bin'은(는) 100MB를 넘어 첨부할 수 없습니다.")).toBeTruthy();
    expect(chips()).toContain("입력.csv");
    expect(chips()).not.toContain("큰파일.bin");
  });

  it("질문에 답하는 중에는 파일을 붙일 수 없다", () => {
    setup({ answerMode: true });
    expect(screen.queryByLabelText("첨부할 파일 고르기")).toBeNull();
    expect(textbox().placeholder).toBe("여기에 쓰면 질문에 대한 답으로 보냅니다");
  });

  it("런이 돌면 중지만, 입력을 기다리면 중지와 보내기를 함께 둔다", async () => {
    const { onStop, user } = setup({ running: true });
    expect(screen.queryByRole("button", { name: "보내기" })).toBeNull();
    await user.click(button("중지"));
    expect(onStop).toHaveBeenCalledOnce();
    cleanup();
    setup({ stoppable: true, disabled: true });
    expect(button("중지").disabled).toBe(false);
    expect(button("보내기").disabled).toBe(true);
  });
});
