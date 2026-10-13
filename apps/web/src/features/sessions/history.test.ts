import { describe, expect, it } from "vitest";
import { contract } from "@/test/contract";
import { applyRunEvents, initialRunState, type TimelineItem } from "@/stores/runStore";
import { historyState } from "./history";
import type { MessageOut } from "./types";

function kinds(items: TimelineItem[] | undefined) {
  return (items ?? []).map((item) => item.kind);
}

function row(partial: Partial<MessageOut> & Pick<MessageOut, "role" | "blocks">): MessageOut {
  return {
    id: partial.id ?? "m1",
    session_id: "s1",
    run_id: partial.run_id ?? "r1",
    seq: partial.seq ?? 1,
    text: partial.text ?? "",
    stop_reason: null,
    files: partial.files ?? [],
    usage: null,
    created_at: "2026-10-13T05:00:00.000Z",
    ...partial,
  };
}

describe("메시지 기록으로 채팅 화면 만들기", () => {
  it("끝난 런: user의 context 블록은 숨기고 도구 카드와 답을 다시 그린다", () => {
    const { messagesDone, detailDone } = contract.approval;
    const state = historyState(messagesDone.items, detailDone);
    expect(kinds(state.items)).toEqual(["user", "tool", "text"]);
    const user = state.items![0] as Extract<TimelineItem, { kind: "user" }>;
    expect(user.text).toBe("파일 목록 보여줘");
    expect(user.text).not.toContain("<context>");
    const toolId = (state.items![1] as Extract<TimelineItem, { kind: "tool" }>).toolCallId;
    expect(state.toolCalls![toolId]).toMatchObject({
      name: "shell",
      input: { command: "echo fake-llm" },
      phase: "done",
      result: "ok",
      restored: true,
    });
    expect(state.toolCalls![toolId].output[0].text).toBe("[종료 코드 0]\nfake-llm\n");
    expect(state.items![2]).toMatchObject({ text: "도구 실행 결과를 확인했습니다.", done: true });
    // 마지막 런의 상태와 사용량을 상태 줄에 보여 준다.
    expect(state).toMatchObject({
      sessionId: detailDone.id,
      runId: detailDone.last_run!.id,
      status: "succeeded",
      step: 2,
      stepLimit: 30,
      model: "fake-model",
      sessionAutoApprove: false,
    });
    expect(state.usage?.inputTokens).toBe(detailDone.last_run!.usage.input_tokens);
  });

  it("승인을 기다리는 런: 그 런의 assistant 행은 빼고 상세의 pending_interaction으로 카드를 띄운다", () => {
    const { messagesWaiting, detailWaiting } = contract.approval;
    const state = historyState(messagesWaiting.items, detailWaiting);
    // 진행 중인 런의 도구 카드는 events?after=0 재생이 만든다.
    expect(kinds(state.items)).toEqual(["user"]);
    expect(state.status).toBe("waiting_input");
    expect(state.runId).toBe(detailWaiting.active_run!.id);
    expect(state.pendingInteraction).toMatchObject({
      kind: "approval",
      tool_name: "shell",
      input: { command: "echo fake-llm" },
      cwd: ".",
      timeout_sec: 120,
      runId: detailWaiting.active_run!.id,
    });
  });

  it("기록 위에 진행 중인 런의 재생을 얹으면 승인 카드가 도구 카드 아래로 돌아온다", () => {
    const { messagesWaiting, detailWaiting, send } = contract.approval;
    const restored = applyRunEvents(
      initialRunState(historyState(messagesWaiting.items, detailWaiting)),
      send.events,
    );
    expect(kinds(restored.items)).toEqual(["user", "tool"]);
    const tool = Object.values(restored.toolCalls)[0];
    expect(tool.phase).toBe("awaiting_approval");
    expect(restored.pendingInteraction?.tool_call_id).toBe(tool.id);
    expect(restored.status).toBe("waiting_input");
    // 재생의 usage를 다시 더하지 않는다(사용량은 재생에서만 센다).
    expect(restored.usage.inputTokens).toBe(1340);
  });

  it("첨부 파일, 바뀐 파일, 질문 답을 메시지 기록에서 되살린다", () => {
    const { messagesDone } = contract.question;
    const state = historyState(messagesDone.items, null);
    expect(kinds(state.items)).toEqual(["user", "text", "tool", "tool", "text"]);
    const user = state.items![0] as Extract<TimelineItem, { kind: "user" }>;
    expect(user.text).toBe("파일 만들고 물어봐");
    expect(user.files).toEqual([
      {
        sessionFileId: contract.question.upload.id,
        name: "입력.csv",
        size: 8,
        downloadable: true,
      },
    ]);
    const tools = Object.values(state.toolCalls!);
    expect(tools.map((tool) => [tool.name, tool.result])).toEqual([
      ["write_file", "ok"],
      ["ask_user", "ok"],
    ]);
    expect(tools[1].output[0].text).toBe("CSV");
    expect(state.files).toEqual([
      {
        path: "outputs/result.csv",
        size: 15,
        sessionFileId: expect.any(String),
        downloadable: true,
        reason: null,
        runId: messagesDone.items[3].run_id,
      },
    ]);
  });

  it("세션 자동 승인이 켜져 있으면 칩을 켠다", () => {
    const state = historyState([], contract.autoApprove.detail);
    expect(state.sessionAutoApprove).toBe(true);
  });

  it("거절된 호출과 결과가 없는 호출을 구분한다", () => {
    const state = historyState(
      [
        row({
          role: "assistant",
          blocks: [
            { type: "tool_use", id: "t1", name: "shell", input: { command: "rm -r x" } },
            { type: "tool_use", id: "t2", name: "shell", input: { command: "dir" } },
            {
              type: "tool_result",
              tool_use_id: "t1",
              content: "사용자가 이 작업을 거절했습니다: 안 돼",
              is_error: false,
            },
          ],
        }),
        row({
          id: "m2",
          seq: 2,
          role: "assistant",
          blocks: [
            { type: "tool_use", id: "t3", name: "read_file", input: { path: "x" } },
            { type: "tool_result", tool_use_id: "t3", content: "없는 파일", is_error: true },
          ],
        }),
      ],
      null,
    );
    expect(Object.values(state.toolCalls!).map((tool) => [tool.id, tool.result])).toEqual([
      ["t1", "rejected"],
      ["t2", "cancelled"],
      ["t3", "error"],
    ]);
  });

  it("같은 도구 호출 id가 다시 나와도 카드를 따로 둔다", () => {
    const use = { type: "tool_use", id: "call_0", name: "list_files", input: {} };
    const state = historyState(
      [
        row({ role: "assistant", blocks: [use] }),
        row({ id: "m2", seq: 2, run_id: "r2", role: "assistant", blocks: [use] }),
      ],
      null,
    );
    expect(Object.keys(state.toolCalls!)).toEqual(["call_0", "call_0@m2"]);
    expect(kinds(state.items)).toEqual(["tool", "tool"]);
  });

  it("요약 행은 구분선으로, 더 앞의 메시지가 있으면 안내로 보여 준다", () => {
    const state = historyState(
      [row({ role: "system_summary", blocks: [{ type: "text", text: "요약" }], text: "요약" })],
      null,
      { hasMore: true },
    );
    expect(state.items).toEqual([
      expect.objectContaining({ kind: "notice", code: "history_truncated" }),
      expect.objectContaining({ kind: "compaction", beforeTokens: null, afterTokens: null }),
    ]);
  });

  it("중지된 마지막 런과 실패한 마지막 런을 타임라인 끝에 알린다", () => {
    const { detailDone } = contract.approval;
    const lastRun = detailDone.last_run!;
    const cancelled = historyState([], {
      ...detailDone,
      last_run: { ...lastRun, status: "cancelled" },
    });
    expect(cancelled.items).toEqual([expect.objectContaining({ code: "stopped" })]);
    const failed = historyState([], {
      ...detailDone,
      last_run: {
        ...lastRun,
        status: "failed",
        error: { code: "model_unavailable", message: "429", retryable: true },
      },
    });
    expect(failed.items).toEqual([
      expect.objectContaining({ kind: "error", code: "model_unavailable", retryable: true }),
    ]);
    expect(failed.error).toEqual({ code: "model_unavailable", message: "429", retryable: true });
  });
});
