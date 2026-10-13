import { describe, expect, it } from "vitest";
import type { DataRunEvent, RunEvent, RunEventOf } from "@/api/run-events";
import { contract } from "@/test/contract";
import { eventFactory, fixtures } from "@/test/events";
import {
  appendOutput,
  applyRunEvent,
  applyRunEvents,
  createRunStore,
  initialRunState,
  TOOL_OUTPUT_LIMIT,
  type RunViewState,
  type TimelineItem,
  type ToolCallView,
} from "./runStore";

const run = (events: readonly RunEvent[]) => applyRunEvents(initialRunState(), events);

function itemsOf<K extends TimelineItem["kind"]>(state: RunViewState, kind: K) {
  return state.items.filter(
    (item): item is Extract<TimelineItem, { kind: K }> => item.kind === kind,
  );
}

function last(events: RunEvent[]): DataRunEvent {
  return events[events.length - 1] as DataRunEvent;
}

// 서버가 POST /runs/{rid}/input을 받으면 남기는 이벤트(설계 4.9). 픽스처의 재개 파일은 그 다음 seq부터다.
function resolved(after: RunEvent[], data: RunEventOf<"interaction.resolved">["data"]): RunEvent {
  const tail = last(after);
  return {
    run_id: tail.run_id,
    session_id: tail.session_id,
    seq: tail.seq + 1,
    ts: tail.ts,
    type: "interaction.resolved",
    data,
  } as RunEvent;
}

describe("runStore 리듀서", () => {
  it("텍스트 답변: 델타를 이어 붙이고 완료 이벤트로 마무리한다", () => {
    const state = run(fixtures.textOnly);
    const completed = fixtures.textOnly.find((event) => event.type === "message.completed");
    const texts = itemsOf(state, "text");
    expect(texts).toHaveLength(1);
    expect(texts[0].done).toBe(true);
    expect(texts[0].text).toBe(
      (completed as Extract<DataRunEvent, { type: "message.completed" }>).data.text,
    );
    expect(state.status).toBe("succeeded");
    expect(state.stepLimit).toBe(30);
    expect(state.model).toBe("gemini-3.8-flash");
    expect(state.usage).toMatchObject({
      inputTokens: 1820,
      outputTokens: 214,
      reasoningTokens: 96,
    });
    expect(state.queuePosition).toBeNull();
    expect(state.startedAt).not.toBeNull();
    expect(state.endedAt).not.toBeNull();
  });

  it("run.queued는 대기열 위치를 남기고 run.started가 실행 상태로 바꾼다", () => {
    const e = eventFactory();
    const queued = run([e("run.queued", { position: 2 })]);
    expect(queued.status).toBe("queued");
    expect(queued.queuePosition).toBe(2);
    const started = applyRunEvent(
      queued,
      e("run.started", {
        step_limit: 10,
        model: "gemini-3.7-flash",
        workspace_instance_id: null,
        resumed: false,
        approval_mode: "off",
      }),
    );
    expect(started).toMatchObject({
      status: "running",
      queuePosition: null,
      stepLimit: 10,
      model: "gemini-3.7-flash",
      approvalMode: "off",
    });
  });

  it("승인 대기: 도구 카드를 승인 대기로 두고 pendingInteraction을 남긴다", () => {
    const state = run(fixtures.approvalWait);
    expect(state.status).toBe("waiting_input");
    expect(state.pendingInteraction).toMatchObject({
      interaction_id: "int_01",
      kind: "approval",
      tool_call_id: "call_02",
      tool_name: "shell",
      runId: last(fixtures.approvalWait).run_id,
    });
    expect(state.toolCalls.call_02.phase).toBe("awaiting_approval");
    expect(state.toolCalls.call_02.input?.command).toContain("Invoke-WebRequest");
    // 입력 델타를 모은 원문과 ready의 input이 같아야 한다.
    expect(JSON.parse(state.toolCalls.call_02.inputText)).toEqual(state.toolCalls.call_02.input);
    // 파일 도구는 실시간 출력 없이 완료 이벤트의 발췌가 전체 출력이다.
    expect(state.toolCalls.call_01).toMatchObject({
      phase: "done",
      result: "ok",
      outputIsExcerpt: false,
    });
    expect(state.toolCalls.call_01.output[0].text).toContain("data.csv");
    expect(itemsOf(state, "tool").map((item) => item.toolCallId)).toEqual(["call_01", "call_02"]);
    expect(state.endedAt).toBeNull();
  });

  it("승인 뒤 재개: 결정을 남기고 출력·파일 변경·완료를 반영한다", () => {
    const decision = resolved(fixtures.approvalWait, {
      interaction_id: "int_01",
      kind: "approval",
      decision: "approve",
      session_auto_approve: false,
    });
    const state = run([...fixtures.approvalWait, decision, ...fixtures.approvalApproved]);
    expect(state.status).toBe("succeeded");
    expect(state.pendingInteraction).toBeNull();
    expect(state.sessionAutoApprove).toBe(false);
    const tool = state.toolCalls.call_02;
    expect(tool).toMatchObject({
      phase: "done",
      result: "ok",
      exitCode: 0,
      outputIsExcerpt: false,
    });
    expect(tool.output.map((chunk) => chunk.stream)).toEqual(["stdout", "stderr"]);
    expect(tool.output[0].text).toBe("상태 코드: 200\r\n");
    expect(state.files).toEqual([
      {
        path: "outputs/status.txt",
        size: 24,
        sessionFileId: "0199d7a0-5c1e-7a00-8000-0000000000f1",
        downloadable: true,
        reason: null,
        runId: last(fixtures.approvalWait).run_id,
      },
    ]);
    expect(itemsOf(state, "interaction")[0]).toMatchObject({
      decision: "approve",
      autoApprove: false,
    });
    expect(state.usage.inputTokens).toBe(7990);
  });

  it("거절: 도구를 거절됨으로 바꾸고 사유를 타임라인에 남긴다", () => {
    const decision = resolved(fixtures.approvalWait, {
      interaction_id: "int_01",
      kind: "approval",
      decision: "reject",
      note: "외부 요청은 하지 마",
    });
    const waiting = run([...fixtures.approvalWait, decision]);
    expect(waiting.toolCalls.call_02).toMatchObject({ phase: "done", result: "rejected" });
    expect(waiting.pendingInteraction).toBeNull();
    expect(itemsOf(waiting, "interaction")[0]).toMatchObject({
      decision: "reject",
      note: "외부 요청은 하지 마",
    });
    const done = applyRunEvents(waiting, fixtures.approvalRejected);
    expect(done.status).toBe("succeeded");
    expect(done.toolCalls.call_02.result).toBe("rejected");
    expect(itemsOf(done, "text").at(-1)?.text).toContain("실행하지 않았습니다");
  });

  it("'이 세션에서는 묻지 않기'로 승인하면 자동 승인이 켜진다", () => {
    const decision = resolved(fixtures.approvalWait, {
      interaction_id: "int_01",
      kind: "approval",
      decision: "approve",
      session_auto_approve: true,
    });
    const state = run([...fixtures.approvalWait, decision]);
    expect(state.sessionAutoApprove).toBe(true);
    expect(state.toolCalls.call_02.phase).toBe("ready");
    expect(itemsOf(state, "interaction")[0].autoApprove).toBe(true);
  });

  it("질문 대기와 답변", () => {
    const waiting = run(fixtures.questionWait);
    expect(waiting.status).toBe("waiting_input");
    expect(waiting.pendingInteraction).toMatchObject({
      kind: "question",
      question: "결과를 어떤 형식으로 저장할까요?",
      options: ["CSV", "JSON", "엑셀(xlsx)"],
    });
    // 질문 도구는 승인 대기가 아니다.
    expect(waiting.toolCalls.call_21.phase).toBe("ready");
    const answer = resolved(fixtures.questionWait, {
      interaction_id: "int_02",
      kind: "question",
      answer: "CSV",
    });
    const done = applyRunEvents(waiting, [answer, ...fixtures.questionAnswered]);
    expect(done.status).toBe("succeeded");
    expect(itemsOf(done, "interaction")[0]).toMatchObject({ answer: "CSV", decision: null });
    expect(done.files.map((file) => [file.path, file.downloadable, file.reason])).toEqual([
      ["outputs/result.csv", true, null],
      ["outputs/raw-dump.bin", false, "too_large"],
    ]);
  });

  it("대기열·복원·사고·재시도·병렬 도구·압축·시간 초과·오류를 모두 반영한다", () => {
    const state = run(fixtures.edgeCases);
    expect(state.status).toBe("failed");
    expect(state.error).toEqual({
      code: "model_unavailable",
      message: "모든 키와 폴백 모델에서 429를 받았다",
      retryable: true,
    });
    expect(itemsOf(state, "error")).toHaveLength(1);
    // 끊긴 스텝의 부분 텍스트와 사고 과정은 버리고 다시 받은 것만 남는다(설계 4.6).
    expect(itemsOf(state, "text").map((item) => item.text)).toEqual([
      "두 파일을 함께 읽어 비교하겠습니다.",
    ]);
    expect(itemsOf(state, "thinking").map((item) => item.text)).toEqual([
      "다시: 두 파일을 함께 읽는다.",
    ]);
    expect(itemsOf(state, "notice").map((item) => [item.code, item.count])).toEqual([
      ["workspace_restoring", null],
      ["workspace_restored", 3],
      ["retrying", 1],
      ["prompt_changed", null],
    ]);
    expect(itemsOf(state, "compaction")[0]).toMatchObject({
      beforeTokens: 212345,
      afterTokens: 8421,
    });
    expect(state.toolCalls.call_31.result).toBe("ok");
    expect(state.toolCalls.call_32.result).toBe("error");
    const slow = state.toolCalls.call_33;
    expect(slow).toMatchObject({
      result: "timeout",
      timedOut: true,
      exitCode: null,
      outputBytes: 284211,
    });
    expect(slow.outputStorageKey).toContain("call_33.txt");
    expect(slow.output.at(-1)?.stream).toBe("stderr");
    expect(state.approvalMode).toBe("off");
    expect(state.usage.inputTokens).toBe(12170);
  });

  it("재접속으로 같은 seq가 다시 오면 버린다", () => {
    const once = run(fixtures.textOnly);
    const twice = applyRunEvents(once, fixtures.textOnly);
    expect(twice).toEqual(once);
  });

  it("델타 없이 완료 이벤트만 오면(영속 이벤트 재생) 전문으로 메시지를 만든다", () => {
    const e = eventFactory();
    const state = run([
      e("message.completed", {
        message_id: "m1",
        text: "전문",
        stop_reason: "end_turn",
        files: [],
      }),
    ]);
    expect(itemsOf(state, "text")).toMatchObject([{ messageId: "m1", text: "전문", done: true }]);
  });

  it("델타를 일부만 받았으면 완료 이벤트의 전문으로 바꾼다", () => {
    const e = eventFactory();
    const state = run([
      e("message.delta", { message_id: "m1", text: "앞부분" }),
      e("tool_call.started", { tool_call_id: "t1", name: "read_file" }),
      e("message.delta", { message_id: "m1", text: "뒷" }),
      e("message.completed", {
        message_id: "m1",
        text: "앞부분 그리고 뒷부분",
        stop_reason: "end_turn",
        files: [],
      }),
    ]);
    expect(itemsOf(state, "text")).toMatchObject([{ text: "앞부분 그리고 뒷부분", done: true }]);
  });

  it("새 런이 시작되면 런 단위 상태만 비우고 타임라인은 이어 간다", () => {
    const first = run(fixtures.textOnly);
    const e = eventFactory("run-2", first.sessionId ?? "");
    const next = applyRunEvents(first, [
      e("run.started", {
        step_limit: 30,
        model: null,
        workspace_instance_id: null,
        resumed: false,
        approval_mode: "all_shell",
      }),
      e("message.delta", { message_id: "m2", text: "둘째 답" }),
    ]);
    expect(next.runId).toBe("run-2");
    expect(next.status).toBe("running");
    expect(next.usage.inputTokens).toBe(0);
    expect(next.model).toBeNull();
    expect(itemsOf(next, "text")).toHaveLength(2);
  });

  it("작업공간 경고·재생성 알림", () => {
    const e = eventFactory();
    const state = run([
      e("workspace.restore_warning", { message: "requests 설치 실패" }),
      e("workspace.recreated", {}),
    ]);
    expect(itemsOf(state, "notice").map((item) => [item.code, item.message])).toEqual([
      ["workspace_restore_warning", "requests 설치 실패"],
      ["workspace_recreated", null],
    ]);
  });

  it("스텝 한도와 변경 파일 잘림을 알린다", () => {
    const e = eventFactory();
    const state = run([
      e("run.completed", {
        status: "succeeded",
        usage_total: { input_tokens: 10, output_tokens: 5 },
        steps: 30,
        limit: "steps",
        changes_truncated: true,
      }),
    ]);
    expect(itemsOf(state, "notice").map((item) => item.code)).toEqual([
      "step_limit",
      "changes_truncated",
    ]);
  });

  it("usage는 스텝마다 더하고 비용이 있으면 함께 더한다", () => {
    const e = eventFactory();
    const usage = {
      step: 1,
      model: "gemini-3.8-flash",
      key_index: 0,
      input_tokens: 100,
      output_tokens: 20,
      estimated: false,
      cost_usd: 0.25,
    };
    const state = run([e("usage", usage), e("usage", { ...usage, step: 2, estimated: true })]);
    expect(state.usage).toMatchObject({
      inputTokens: 200,
      outputTokens: 40,
      costUsd: 0.5,
      estimated: true,
    });
  });

  it("실제 서버 이벤트: 메시지 스트림은 run.queued(seq 1)로 시작하고 재개 스트림은 interaction.resolved로 시작한다", () => {
    const { send, resume } = contract.approval;
    expect(send.events[0]).toMatchObject({ seq: 1, type: "run.queued" });
    expect(resume[0].type).toBe("interaction.resolved");
    const waiting = run(send.events);
    expect(waiting).toMatchObject({ status: "waiting_input", runId: send.runId, step: 1 });
    const done = applyRunEvents(waiting, resume);
    expect(done).toMatchObject({ status: "succeeded", step: 2, pendingInteraction: null });
    expect(itemsOf(done, "text").map((item) => item.text)).toEqual([
      "도구 실행 결과를 확인했습니다.",
    ]);
    expect(done.usage.inputTokens).toBe(2712);
  });

  it("중지로 끝난 런: 결과가 없는 도구는 취소됨으로 닫고 중지 알림을 남긴다", () => {
    const { send, after } = contract.stopWaiting;
    const state = run([...send, ...after]);
    expect(state.status).toBe("cancelled");
    expect(state.pendingInteraction).toBeNull();
    expect(Object.values(state.toolCalls)[0]).toMatchObject({ phase: "done", result: "cancelled" });
    expect(itemsOf(state, "notice").map((item) => item.code)).toEqual(["stopped"]);
  });

  it("입력 대기 시간이 지나 취소되면 이유를 알린다", () => {
    const e = eventFactory();
    const state = run([
      e("run.completed", {
        status: "cancelled",
        usage_total: {},
        steps: 1,
        error: { code: "input_timeout", message: "24시간 안에 응답이 없어 취소했다" },
      }),
    ]);
    expect(itemsOf(state, "notice")).toMatchObject([
      { code: "input_timeout", message: "24시간 안에 응답이 없어 취소했다" },
    ]);
  });

  it("run.error를 놓쳤어도 run.completed(failed)의 error로 오류를 보여 주고, 둘 다 오면 한 번만 보여 준다", () => {
    const e = eventFactory();
    const error = { code: "model_unavailable", message: "429", retryable: true };
    const completed = e("run.completed", {
      status: "failed",
      usage_total: {},
      steps: 1,
      error,
    });
    const onlyCompleted = run([completed]);
    expect(onlyCompleted.error).toEqual(error);
    expect(itemsOf(onlyCompleted, "error")).toHaveLength(1);
    const f = eventFactory();
    const both = run([
      f("run.error", error),
      f("run.completed", { status: "failed", usage_total: {}, steps: 1, error }),
    ]);
    expect(both.status).toBe("failed");
    expect(itemsOf(both, "error")).toHaveLength(1);
  });

  it("message.completed의 files를 바뀐 파일 목록에 합치고 같은 경로는 하나만 둔다", () => {
    const { send, answer } = contract.question;
    const state = run([...send, ...answer]);
    expect(state.files).toEqual([
      {
        path: "outputs/result.csv",
        size: 15,
        sessionFileId: expect.any(String),
        downloadable: true,
        reason: null,
        runId: send[0].run_id,
      },
    ]);
    const e = eventFactory();
    const fromMessage = run([
      e("message.completed", {
        message_id: "m1",
        text: "끝",
        stop_reason: "end_turn",
        files: [{ path: "outputs/a.txt", size: 3, downloadable: true, session_file_id: "f1" }],
      }),
    ]);
    expect(fromMessage.files).toMatchObject([{ path: "outputs/a.txt", sessionFileId: "f1" }]);
  });

  it("ping과 모르는 이벤트는 상태를 바꾸지 않는다", () => {
    const state = run(fixtures.textOnly);
    expect(applyRunEvent(state, { type: "ping" })).toBe(state);
    const unknown = {
      ...last(fixtures.textOnly),
      seq: 999,
      type: "something.new",
    } as unknown as RunEvent;
    expect(applyRunEvent(state, unknown)).toBe(state);
  });
});

describe("도구 출력 링 버퍼", () => {
  const base: ToolCallView = run([
    eventFactory()("tool_call.started", { tool_call_id: "t1", name: "shell" }),
  ]).toolCalls.t1;

  it("같은 스트림 조각은 이어 붙이고 스트림이 바뀌면 나눈다", () => {
    let tool = appendOutput(base, "stdout", "a");
    tool = appendOutput(tool, "stdout", "b");
    tool = appendOutput(tool, "stderr", "c");
    expect(tool.output).toEqual([
      { stream: "stdout", text: "ab" },
      { stream: "stderr", text: "c" },
    ]);
    expect(tool.outputChars).toBe(3);
  });

  it("상한을 넘으면 앞에서부터 버리고 버린 양을 센다", () => {
    let tool = appendOutput(base, "stdout", "x".repeat(6), 10);
    tool = appendOutput(tool, "stderr", "y".repeat(7), 10);
    expect(tool.output).toEqual([
      { stream: "stdout", text: "xxx" },
      { stream: "stderr", text: "yyyyyyy" },
    ]);
    expect(tool.outputChars).toBe(10);
    expect(tool.droppedChars).toBe(3);
    tool = appendOutput(tool, "stderr", "z".repeat(4), 10);
    expect(tool.output).toEqual([{ stream: "stderr", text: "yyyyyyzzzz" }]);
    expect(tool.droppedChars).toBe(7);
  });

  it("200KB 넘게 흘려도 화면 버퍼는 상한을 지킨다", () => {
    const e = eventFactory();
    const chunk = "가".repeat(10 * 1024);
    const events: RunEvent[] = [e("tool_call.started", { tool_call_id: "t1", name: "shell" })];
    for (let i = 0; i < 30; i += 1) {
      events.push(
        e("tool_call.output_delta", { tool_call_id: "t1", stream: "stdout", text: chunk }),
      );
    }
    const tool = run(events).toolCalls.t1;
    expect(tool.outputChars).toBe(TOOL_OUTPUT_LIMIT);
    expect(tool.droppedChars).toBe(30 * chunk.length - TOOL_OUTPUT_LIMIT);
    expect(tool.phase).toBe("running");
  });
});

describe("createRunStore", () => {
  function manualFrames() {
    const pending: (() => void)[] = [];
    return {
      schedule: (callback: () => void) => {
        pending.push(callback);
        return () => {
          const index = pending.indexOf(callback);
          if (index >= 0) pending.splice(index, 1);
        };
      },
      runFrame: () => pending.splice(0).forEach((callback) => callback()),
      get size() {
        return pending.length;
      },
    };
  }

  it("델타는 다음 프레임에 모아 반영하고 다른 이벤트는 쌓인 것과 함께 바로 반영한다", () => {
    const frames = manualFrames();
    const store = createRunStore({ schedule: frames.schedule });
    const { actions } = store.getState();
    const e = eventFactory();
    actions.applyEvent(e("step.started", { step: 1 }));
    expect(store.getState().step).toBe(1);

    actions.applyEvent(e("message.delta", { message_id: "m", text: "가" }));
    actions.applyEvent(e("message.delta", { message_id: "m", text: "나" }));
    expect(itemsOf(store.getState(), "text")).toHaveLength(0);
    expect(frames.size).toBe(1);
    frames.runFrame();
    expect(itemsOf(store.getState(), "text")[0].text).toBe("가나");

    actions.applyEvent(e("message.delta", { message_id: "m", text: "다" }));
    actions.applyEvent(
      e("message.completed", {
        message_id: "m",
        text: "가나다",
        stop_reason: "end_turn",
        files: [],
      }),
    );
    expect(itemsOf(store.getState(), "text")).toMatchObject([{ text: "가나다", done: true }]);
    expect(frames.size).toBe(0);
  });

  it("reset은 반영하지 않은 델타를 버린다", () => {
    const frames = manualFrames();
    const store = createRunStore({ schedule: frames.schedule });
    const e = eventFactory();
    store.getState().actions.applyEvent(e("message.delta", { message_id: "m", text: "버릴 글" }));
    store.getState().actions.reset();
    frames.runFrame();
    expect(store.getState().items).toEqual([]);
    expect(store.getState().status).toBe("idle");
  });

  it("addUserMessage는 대기 중인 델타를 먼저 반영하고 내 메시지를 붙인다", () => {
    const frames = manualFrames();
    const store = createRunStore({ schedule: frames.schedule });
    const e = eventFactory();
    store.getState().actions.applyEvent(e("message.delta", { message_id: "m", text: "답" }));
    store.getState().actions.addUserMessage("다음 질문");
    expect(store.getState().items.map((item) => item.kind)).toEqual(["text", "user"]);
  });
});
