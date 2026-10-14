import { describe, expect, it } from "vitest";
import { inspectRun, stepTokens } from "./inspector";
import type { RunEventOut } from "./types";

function events(...items: [string, Record<string, unknown>][]): RunEventOut[] {
  return items.map(([type, data], index) => ({
    run_id: "run-1",
    session_id: "session-1",
    seq: index + 1,
    ts: new Date(Date.UTC(2026, 9, 13, 5, 0, index)).toISOString(),
    type,
    data,
  }));
}

const usage = (step: number, input: number, output: number, cost: number | null) => ({
  step,
  model: "gemini-3.8-flash",
  key_index: 0,
  input_tokens: input,
  output_tokens: output,
  estimated: false,
  cost_usd: cost,
});

// 승인을 받아 이어 간 런: 스텝 1에서 셸 승인 대기 → 승인(세션 자동 승인) → 실행 → 스텝 2에서 답.
const approvedRun = events(
  ["run.queued", { position: 1 }],
  ["run.started", { step_limit: 30, model: null, resumed: false, approval_mode: "all_shell" }],
  ["workspace.restored", { snapshot_id: "s1", packages: 2 }],
  ["step.started", { step: 1 }],
  ["tool_call.started", { tool_call_id: "call_1", name: "shell" }],
  ["tool_call.ready", { tool_call_id: "call_1", name: "shell", input: { command: "dir" } }],
  ["usage", usage(1, 100, 20, 0.001)],
  [
    "run.waiting_input",
    { interaction_id: "i1", kind: "approval", tool_call_id: "call_1", tool_name: "shell" },
  ],
  ["run.completed", { status: "waiting_input", usage_total: {}, steps: 1 }],
  [
    "interaction.resolved",
    { interaction_id: "i1", kind: "approval", decision: "approve", session_auto_approve: true },
  ],
  ["run.queued", { position: 1 }],
  ["run.resumed", { interaction_id: "i1" }],
  ["run.started", { step_limit: 30, model: null, resumed: true, approval_mode: "off" }],
  [
    "tool_call.completed",
    {
      tool_call_id: "call_1",
      status: "ok",
      exit_code: 0,
      timed_out: false,
      duration_ms: 420,
      peak_memory_bytes: 5_000_000,
      output_excerpt: "a.txt\n",
      output_bytes: 6,
      output_storage_key: null,
    },
  ],
  ["step.started", { step: 2 }],
  ["run.retrying", { step: 2, reason: "model_stream_interrupted", attempt: 1 }],
  ["usage", usage(2, 150, 30, null)],
  ["message.completed", { message_id: "m2", text: "끝났습니다", stop_reason: "end_turn" }],
  ["file.changed", { path: "outputs/r.csv", size: 12, session_file_id: "f1", downloadable: true }],
  ["run.completed", { status: "succeeded", usage_total: {}, steps: 2 }],
);

describe("런 인스펙터 모델", () => {
  it("스텝마다 도구 호출·사용량·메시지를 모은다", () => {
    const run = inspectRun(approvedRun);

    expect(run.steps.map((step) => step.step)).toEqual([1, 2]);
    const [first, second] = run.steps;
    expect(first.toolCalls.map((tool) => tool.id)).toEqual(["call_1"]);
    expect(first.toolCalls[0]).toMatchObject({
      name: "shell",
      input: { command: "dir" },
      status: "ok",
      exitCode: 0,
      durationMs: 420,
      peakMemoryBytes: 5_000_000,
      outputExcerpt: "a.txt\n",
      outputStored: false,
    });
    expect(first.usage).toMatchObject({ inputTokens: 100, outputTokens: 20, costUsd: 0.001 });
    expect(stepTokens(second)).toBe(180);
    expect(second.messages).toEqual([
      { id: "m2", text: "끝났습니다", stopReason: "end_turn", files: [] },
    ]);
    expect(second.notices.map((notice) => notice.type)).toEqual(["run.retrying"]);
    expect(run.preamble.map((notice) => notice.type)).toEqual(["workspace.restored"]);
    expect(run.files).toEqual([
      {
        path: "outputs/r.csv",
        size: 12,
        sessionFileId: "f1",
        downloadable: true,
        reason: null,
      },
    ]);
  });

  it("승인 결정을 해당 도구 호출에 붙이고 마지막 상태를 남긴다", () => {
    const run = inspectRun(approvedRun);

    const tool = run.steps[0].toolCalls[0];
    expect(tool.interaction).toMatchObject({
      interactionId: "i1",
      kind: "approval",
      resolution: { decision: "approve", autoApprove: true, note: null },
    });
    expect(run.interactions).toHaveLength(1);
    expect(run.resumes).toBe(1);
    expect(run.started?.resumed).toBe(true);
    expect(run.completed?.status).toBe("succeeded");
    expect(run.error).toBeNull();
  });

  it("거절·질문·오류와 순서가 섞인 기록도 그린다", () => {
    const shuffled = events(
      ["step.started", { step: 1 }],
      [
        "tool_call.ready",
        { tool_call_id: "call_q", name: "ask_user", input: { question: "이름은?" } },
      ],
      [
        "run.waiting_input",
        { interaction_id: "q1", kind: "question", tool_call_id: "call_q", question: "이름은?" },
      ],
      ["interaction.resolved", { interaction_id: "q1", kind: "question", answer: "지우" }],
      [
        "run.waiting_input",
        { interaction_id: "a1", kind: "approval", tool_call_id: "call_x", tool_name: "shell" },
      ],
      [
        "interaction.resolved",
        { interaction_id: "a1", kind: "approval", decision: "reject", note: "위험하다" },
      ],
      [
        "tool_call.completed",
        {
          tool_call_id: "call_x",
          status: "rejected",
          timed_out: false,
          duration_ms: 0,
          output_excerpt: "",
          output_bytes: 0,
          output_storage_key: "tool-outputs/r/call_x.txt",
        },
      ],
      ["run.error", { code: "model_unavailable", message: "모든 키가 429", retryable: true }],
      ["run.completed", { status: "failed", usage_total: {}, steps: 1 }],
    ).reverse();

    const run = inspectRun(shuffled);

    const [question, rejected] = run.steps[0].toolCalls;
    expect(question.interaction?.resolution?.answer).toBe("지우");
    // 승인 대기 이벤트에만 나온 도구 호출도 이름을 받는다.
    expect(rejected).toMatchObject({ id: "call_x", name: "shell", status: "rejected" });
    expect(rejected.outputStored).toBe(true);
    expect(rejected.interaction?.resolution).toMatchObject({
      decision: "reject",
      note: "위험하다",
    });
    expect(run.error).toMatchObject({ code: "model_unavailable", retryable: true });
    expect(run.steps[0].notices.map((notice) => notice.type)).toEqual(["run.error"]);
    expect(run.completed?.status).toBe("failed");
  });

  it("스텝 표시가 없어도 도구 호출을 버리지 않는다", () => {
    const run = inspectRun(
      events([
        "tool_call.completed",
        {
          tool_call_id: "orphan",
          status: "error",
          timed_out: true,
          duration_ms: 120_000,
          output_excerpt: "시간 초과",
          output_bytes: 9,
        },
      ]),
    );

    expect(run.steps).toHaveLength(1);
    expect(run.steps[0].step).toBe(0);
    expect(run.steps[0].toolCalls[0]).toMatchObject({ id: "orphan", timedOut: true });
    expect(inspectRun([]).steps).toEqual([]);
  });

  it("call_agent 호출에 하위 에이전트 런을 붙이고 child_run 이벤트는 알림으로 따로 두지 않는다", () => {
    const run = inspectRun(
      events(
        ["step.started", { step: 1 }],
        ["tool_call.started", { tool_call_id: "call_a", name: "call_agent" }],
        [
          "child_run.started",
          {
            tool_call_id: "call_a",
            child_run_id: "child-1",
            child_session_id: "child-session",
            agent_id: "agent-2",
            agent_slug: "researcher",
            agent_name: "조사",
            version: "draft",
            depth: 1,
            result_format: "text",
            task_preview: "조사한다",
          },
        ],
        [
          "child_run.completed",
          {
            tool_call_id: "call_a",
            child_run_id: "child-1",
            status: "succeeded",
            steps: 2,
            usage_total: {},
            duration_ms: 900,
            files: 0,
          },
        ],
      ),
    );
    const [call] = run.steps[0].toolCalls;
    expect(call.child).toEqual({
      runId: "child-1",
      sessionId: "child-session",
      agentSlug: "researcher",
      agentName: "조사",
      version: "draft",
      status: "succeeded",
    });
    expect(run.steps[0].notices).toEqual([]);
  });
});
