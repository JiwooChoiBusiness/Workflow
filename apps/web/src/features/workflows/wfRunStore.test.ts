import { describe, expect, it } from "vitest";
import type { WfEvent } from "@/api/run-events";
import { chatContract } from "@/test/chatContract";
import { secondNodesContract } from "@/test/secondNodesContract";
import { wfContract } from "@/test/wfContract";
import {
  applyWfEvent,
  applyWfEvents,
  createWfRunStore,
  finishedNodeCount,
  initialWfRunState,
  mergeNodeRuns,
  NODE_TEXT_LIMIT,
  pendingFrom,
  runningNode,
  stateFromRun,
  type WfRunViewState,
} from "./wfRunStore";

const run = (events: readonly WfEvent[]) => applyWfEvents(initialWfRunState(), events);

function byNode(state: WfRunViewState, nodeId: string) {
  return Object.values(state.nodeRuns).filter((item) => item.nodeId === nodeId);
}

function latest(state: WfRunViewState, nodeId: string) {
  const id = state.latestByNode[nodeId];
  if (!id) throw new Error(`${nodeId}의 노드 런이 없다`);
  return state.nodeRuns[id]!;
}

describe("wfRunStore 리듀서(실제 서버 이벤트)", () => {
  it("선형 런: 노드 런이 실행 순서대로 쌓이고 end 출력과 상태로 끝난다", () => {
    const { events, runId } = wfContract.linear;
    const state = run(events);
    expect(state.runId).toBe(runId);
    expect(state.status).toBe("succeeded");
    expect(state.order.map((id) => state.nodeRuns[id]!.nodeId)).toEqual([
      "start",
      "template_1",
      "end_1",
    ]);
    expect(Object.values(state.nodeRuns).every((item) => item.status === "succeeded")).toBe(true);
    expect(latest(state, "template_1")).toMatchObject({
      nodeType: "template",
      index: 1,
      outputsPreview: { output: "안녕, 세계" },
      iterationId: null,
    });
    expect(state.outputs).toEqual({ greeting: "안녕, 세계" });
    expect(state.nodeCount).toBe(3);
    expect(state.inputsPreview).toEqual({ q: "세계" });
    expect(state.startedAt).not.toBeNull();
    expect(state.endedAt).toBe(events[events.length - 1]!.ts);
    expect(state.lastSeq).toBe(events[events.length - 1]!.seq);
    expect(finishedNodeCount(state)).toBe(3);
  });

  it("분기: 건너뛴 가지는 시작 없이 skipped로 남고 llm 델타·사용량이 노드에 붙는다", () => {
    const state = run(wfContract.branch.events);
    expect(latest(state, "if_else_1").branch).toBe("case_fake");
    for (const nodeId of ["template_no", "end_no"]) {
      expect(latest(state, nodeId)).toMatchObject({
        status: "skipped",
        nodeType: null,
        startedAt: null,
        iterationId: null,
      });
    }
    const llm = latest(state, "llm_1");
    expect(llm.text).toBe("가짜 응답: 분기 질문");
    expect(llm.usage).toMatchObject({ inputTokens: 14, outputTokens: 4, estimated: true });
    expect(state.usage).toMatchObject({ inputTokens: 14, outputTokens: 4 });
    expect(state.outputs).toEqual({ answer: "예: 가짜 응답: 분기 질문" });
    // 건너뛴 노드는 끝난 수에 넣지 않는다.
    expect(finishedNodeCount(state)).toBe(5);
  });

  it("node.usage는 런 합계에 더하고 workflow.completed가 합계를 바로잡는다", () => {
    const events = wfContract.branch.events;
    const usageAt = events.findIndex((event) => event.type === "node.usage");
    const midway = run(events.slice(0, usageAt + 1));
    expect(midway.usage.inputTokens).toBe(14);
    expect(midway.status).toBe("running");
    // 같은 이벤트가 재접속으로 다시 와도 두 번 세지 않는다.
    const replayed = applyWfEvents(midway, events.slice(0, usageAt + 1));
    expect(replayed.usage.inputTokens).toBe(14);
    expect(replayed).toBe(midway);
  });

  it("iteration: 안쪽 노드 런이 반복 순번과 함께 쌓이고 진행·완료가 기록된다", () => {
    const state = run(wfContract.iteration.events);
    const inner = byNode(state, "template_3");
    expect(inner.map((item) => [item.iterationId, item.iterationIndex])).toEqual([
      ["iteration_1", 0],
      ["iteration_1", 1],
      ["iteration_1", 2],
    ]);
    expect(inner.map((item) => item.outputsPreview)).toEqual([
      { output: "0: x-1" },
      { output: "1: x-2" },
      { output: "2: x-3" },
    ]);
    expect(state.iterations.iteration_1).toMatchObject({
      total: 3,
      index: 2,
      count: 3,
      failedCount: 0,
    });
    expect(latest(state, "iteration_1").status).toBe("succeeded");
    expect(state.outputs).toEqual({ items: ["0: x-1", "1: x-2", "2: x-3"] });
  });

  it("iteration 진행 중: iteration.next마다 순번이 바뀐다", () => {
    const events = wfContract.iteration.events;
    const second = events.findIndex(
      (event) => event.type === "iteration.next" && event.data.index === 1,
    );
    const state = run(events.slice(0, second + 1));
    expect(state.iterations.iteration_1).toMatchObject({ total: 3, index: 1, count: null });
    expect(latest(state, "iteration_1").status).toBe("running");
  });

  it("단일 노드 실행: 노드 하나만 돌고 런 출력이 그 노드의 출력이다", () => {
    const state = run(wfContract.singleNode.events);
    expect(state.order).toHaveLength(1);
    expect(latest(state, "llm_1").text).toBe("가짜 응답: 단독 실행");
    expect(state.outputs).toMatchObject({ text: "가짜 응답: 단독 실행", json: null });
  });

  it("실패: 노드 오류와 런 오류가 남는다", () => {
    const state = run(wfContract.failure.events);
    expect(state.status).toBe("failed");
    expect(latest(state, "code_1")).toMatchObject({
      status: "failed",
      error: { code: "code_error", message: "ValueError: 일부러 실패", retryable: true },
    });
    expect(state.error).toEqual({
      code: "code_error",
      message: "ValueError: 일부러 실패",
      nodeId: "code_1",
      retryable: true,
      retryAt: null,
    });
    expect(state.outputs).toBeNull();
  });

  it("agent 노드 승인 대기 → 승인 → 같은 노드 런으로 재개되어 끝난다", () => {
    const { events, resume, waiting, done } = wfContract.agent;
    const paused = run(events);
    expect(paused.status).toBe("waiting_input");
    expect(paused.endedAt).toBeNull();
    const agent = latest(paused, "agent_1");
    expect(agent.status).toBe("waiting_input");
    expect(agent.childRunId).toBe(waiting.pending_interaction?.child_run_id);
    expect(agent.childSessionId).not.toBeNull();
    expect(agent.steps).toBe(0);
    expect(paused.pendingInteraction).toMatchObject({
      nodeId: "agent_1",
      childRunId: waiting.pending_interaction?.child_run_id,
      interaction: { kind: "approval", tool_name: "shell" },
    });

    const resumed = applyWfEvents(paused, resume);
    expect(resumed.status).toBe("succeeded");
    expect(resumed.pendingInteraction).toBeNull();
    expect(resumed.resumed).toBe(true);
    // 재개된 노드 런은 같은 id라 카드가 하나다.
    expect(byNode(resumed, "agent_1")).toHaveLength(1);
    expect(latest(resumed, "agent_1")).toMatchObject({ status: "succeeded", steps: 1 });
    expect(latest(resumed, "agent_1").startedAt).toBe(agent.startedAt);
    expect(resumed.startedAt).toBe(paused.startedAt);
    expect(resumed.outputs).toEqual(done.outputs);
    expect(resumed.order).toHaveLength(3);
  });

  it("재개 직후 큐에 들어오면 기다리던 승인을 지운다", () => {
    const { events, resume } = wfContract.agent;
    const queued = applyWfEvent(run(events), resume[0]!);
    expect(queued.status).toBe("queued");
    expect(queued.pendingInteraction).toBeNull();
  });

  it("다른 런의 이벤트가 오면 그 런으로 새로 시작한다", () => {
    const first = run(wfContract.linear.events);
    const next = applyWfEvent(first, wfContract.branch.events[0]!);
    expect(next.runId).toBe(wfContract.branch.runId);
    expect(next.order).toEqual([]);
    expect(next.status).toBe("queued");
  });

  it("런 상세(이벤트가 지워진 런)와 iteration 안쪽 목록으로도 그린다", () => {
    const { detail, children } = wfContract.iteration;
    let state = stateFromRun(detail, detail.node_runs);
    expect(state.status).toBe("succeeded");
    expect(state.order.map((id) => state.nodeRuns[id]!.nodeId)).toEqual([
      "start",
      "code_1",
      "iteration_1",
      "end_1",
    ]);
    state = mergeNodeRuns(state, children.items);
    expect(byNode(state, "template_3").map((item) => item.iterationIndex)).toEqual([0, 1, 2]);
    // 이미 있는 노드 런은 다시 넣지 않는다.
    expect(mergeNodeRuns(state, children.items)).toBe(state);

    const waiting = stateFromRun(wfContract.agent.waiting, wfContract.agent.waiting.node_runs);
    expect(waiting.pendingInteraction?.kind).toBe("approval");
    expect(waiting.pendingInteraction?.interaction?.kind).toBe("approval");
    expect(latest(waiting, "agent_1").childRunId).toBe(
      wfContract.agent.waiting.pending_interaction?.child_run_id,
    );
  });

  it("저장된 이벤트로 다시 그리면 자식 런 id를 노드 런 요약에서 채운다", () => {
    const { events, waiting } = wfContract.agent;
    // node.progress는 저장하지 않으므로 다시 받은 이벤트에는 없다.
    const replayed = run(events.filter((event) => event.type !== "node.progress"));
    expect(latest(replayed, "agent_1").childRunId).toBeNull();
    const merged = mergeNodeRuns(replayed, waiting.node_runs);
    expect(latest(merged, "agent_1").childRunId).toBe(waiting.pending_interaction?.child_run_id);
    expect(merged.order).toEqual(replayed.order);
  });

  it("node.text_delta는 상한까지만 모은다", () => {
    const [queued, started, startNode, startDone, llmStarted] = wfContract.branch.events;
    let state = run([queued!, started!, startNode!, startDone!, llmStarted!]);
    const llmRunId = state.latestByNode.llm_1!;
    const big = "가".repeat(NODE_TEXT_LIMIT - 10);
    let seq = llmStarted!.seq;
    for (const text of [big, "나".repeat(20), "다"]) {
      seq += 1;
      state = applyWfEvent(state, {
        ...llmStarted!,
        seq,
        type: "node.text_delta",
        data: { node_id: "llm_1", node_run_id: llmRunId, text },
      } as WfEvent);
    }
    expect(state.nodeRuns[llmRunId]!.text).toHaveLength(NODE_TEXT_LIMIT);
    expect(state.nodeRuns[llmRunId]!.textDropped).toBe(true);
  });
});

describe("wfRunStore 리듀서(챗플로우 답)", () => {
  it("answer.delta를 이어 붙이고 answer.completed가 런 전체의 답으로 바꾼다", () => {
    const [turn] = chatContract.basic.turns;
    const state = run(turn!.events);
    expect(state.answerText).toBe("가짜 응답: 첫 질문");
    expect(state.answerResumedAt).toBeNull();
  });

  it("재개 스트림을 본 때의 답 길이를 남기고, 런 전체의 답을 받으면 지운다", () => {
    const { events, resume } = chatContract.approval;
    const waiting = run(events);
    const prefix = waiting.answerText;
    expect(prefix).not.toBe("");
    const started = resume.findIndex((event) => event.type === "workflow.started");
    const resumed = applyWfEvents(waiting, resume.slice(0, started + 1));
    expect(resumed.answerResumedAt).toBe(prefix.length);
    const done = applyWfEvents(resumed, resume.slice(started + 1));
    expect(done.answerResumedAt).toBeNull();
    expect(done.answerText).toBe(chatContract.approval.doneMessages.items[1]!.text);
  });
});

describe("wfRunStore 리듀서(2차 노드: loop와 human_input 폼)", () => {
  const { events, resume, waiting, runId } = secondNodesContract;

  it("폼 대기를 kind=form으로 두고 런 상세에서도 같은 폼을 읽는다", () => {
    const state = run(events);
    expect(state.status).toBe("waiting_input");
    const pending = state.pendingInteraction;
    expect(pending?.kind).toBe("form");
    expect(pending?.interaction).toBeNull();
    expect(pending?.nodeId).toBe("ask");
    expect(pending?.form?.description).toBe("가장 큰 값: 5, 루프 3회");
    expect(pending?.form?.fields.map((field) => field.name)).toEqual(["ok", "note"]);

    const fromRest = stateFromRun(waiting, waiting.node_runs);
    expect(fromRest.runId).toBe(runId);
    expect(fromRest.pendingInteraction?.kind).toBe("form");
    expect(fromRest.pendingInteraction?.form?.form_id).toBe(pending?.form?.form_id);
  });

  it("loop는 iteration 이벤트로 회차를 세고 안쪽 노드 런을 회차별로 둔다", () => {
    const state = run(events);
    expect(state.iterations.loop_1).toMatchObject({ total: 5, count: 3, failedCount: 0 });
    expect(byNode(state, "add").map((item) => [item.iterationId, item.iterationIndex])).toEqual([
      ["loop_1", 0],
      ["loop_1", 1],
      ["loop_1", 2],
    ]);
    // 안쪽 노드가 도는 동안 상태 표시줄에는 loop가 아니라 안쪽 노드 이름이 보인다.
    const inner = events.findIndex(
      (event) => event.type === "node.started" && event.data.node_id === "tpl",
    );
    expect(runningNode(run(events.slice(0, inner + 1)))).toBe("tpl");
  });

  it("폼에 답해 재개되면 대기를 지우고 출력으로 끝난다", () => {
    const done = applyWfEvents(run(events), resume);
    expect(done.status).toBe("succeeded");
    expect(done.pendingInteraction).toBeNull();
    expect(done.outputs).toMatchObject({ first: 5, count: 3, rounds: 3, ok: true });
    expect(latest(done, "ask").status).toBe("succeeded");
  });

  it("kind가 없는 예전 대기는 interaction으로 가르고, 둘 다 없으면 대기로 두지 않는다", () => {
    const interaction = { interaction_id: "i1", kind: "question" as const, question: "?" };
    expect(pendingFrom({ node_id: "agent_1", interaction })?.kind).toBe("question");
    expect(pendingFrom({ node_id: "agent_1", interaction: null, form: null })).toBeNull();
  });
});

describe("wfRunStore", () => {
  it("델타는 다음 프레임에 모아 반영하고 다른 이벤트가 오면 함께 반영한다", () => {
    const callbacks: (() => void)[] = [];
    const store = createWfRunStore({
      schedule: (callback) => {
        callbacks.push(callback);
        return () => undefined;
      },
    });
    const events = wfContract.branch.events;
    const deltaAt = events.findIndex((event) => event.type === "node.text_delta");
    store.getState().actions.applyEvents(events.slice(0, deltaAt));
    store.getState().actions.applyEvent(events[deltaAt]!);
    expect(store.getState().nodeRuns[store.getState().latestByNode.llm_1!]!.text).toBe("");
    expect(callbacks).toHaveLength(1);
    store.getState().actions.applyEvent(events[deltaAt + 1]!);
    expect(store.getState().nodeRuns[store.getState().latestByNode.llm_1!]!.text).toBe(
      "가짜 응답: 분기 질문",
    );
    store.getState().actions.reset();
    expect(store.getState().runId).toBeNull();
    expect(store.getState().order).toEqual([]);
  });
});

// 손으로 만든 이벤트. 서버가 내는 모양에서 이 시험이 보는 칸만 채운다.
function wfEvent(seq: number, type: WfEvent["type"], data: Record<string, unknown>): WfEvent {
  return {
    run_id: "run-k",
    session_id: null,
    seq,
    ts: `2026-11-03T00:00:${String(seq).padStart(2, "0")}Z`,
    type,
    data,
  } as unknown as WfEvent;
}

const waitingForm = [
  wfEvent(1, "workflow.started", {
    kind: "workflow",
    inputs_preview: {},
    truncated: false,
    resumed: false,
    node_count: 3,
  }),
  wfEvent(2, "node.started", {
    node_run_id: "nr-start",
    node_id: "start",
    node_type: "start",
    title: "시작",
    index: 0,
    attempt: 1,
  }),
  wfEvent(3, "node.completed", {
    node_run_id: "nr-start",
    node_id: "start",
    status: "succeeded",
    duration_ms: 1,
  }),
  wfEvent(4, "node.started", {
    node_run_id: "nr-form",
    node_id: "human_input_1",
    node_type: "human_input",
    title: "사람 입력",
    index: 1,
    attempt: 1,
  }),
  wfEvent(5, "node.completed", {
    node_run_id: "nr-form",
    node_id: "human_input_1",
    status: "waiting_input",
    duration_ms: 2,
  }),
  wfEvent(6, "workflow.waiting_input", {
    node_id: "human_input_1",
    kind: "form",
    form: { description: "", fields: [] },
  }),
  wfEvent(7, "workflow.completed", {
    status: "waiting_input",
    usage_total: {},
    node_runs: 2,
    duration_ms: 3,
  }),
];

describe("wfRunStore 끝난 런의 노드 줄(2단계 후속 6)", () => {
  it("입력을 기다리던 런을 중지하면 기다리던 노드 런을 cancelled로 닫는다", () => {
    const waiting = run(waitingForm);
    expect(latest(waiting, "human_input_1").status).toBe("waiting_input");
    // 서버는 중지할 때 노드 런 행만 고치고 노드 이벤트 없이 workflow.completed(cancelled)만 낸다.
    const stopped = applyWfEvent(
      waiting,
      wfEvent(8, "workflow.completed", {
        status: "cancelled",
        usage_total: {},
        node_runs: 2,
        duration_ms: 3,
      }),
    );
    expect(stopped.status).toBe("cancelled");
    expect(latest(stopped, "human_input_1")).toMatchObject({
      status: "cancelled",
      endedAt: "2026-11-03T00:00:08Z",
    });
    expect(latest(stopped, "start").status).toBe("succeeded");
    expect(finishedNodeCount(stopped)).toBe(2);
  });

  it("대기 시간이 지나 실패하면 기다리던 노드 런을 런의 오류로 닫는다", () => {
    const failed = applyWfEvent(
      run(waitingForm),
      wfEvent(8, "workflow.completed", {
        status: "failed",
        usage_total: {},
        node_runs: 2,
        duration_ms: 3,
        error: { code: "input_timeout", message: "입력 대기 시간이 지났다", retryable: false },
      }),
    );
    expect(latest(failed, "human_input_1")).toMatchObject({
      status: "failed",
      error: { code: "input_timeout", message: "입력 대기 시간이 지났다", retryable: false },
    });
  });

  it("입력 대기로 끝난 런은 노드 줄을 그대로 둔다", () => {
    expect(latest(run(waitingForm), "human_input_1").status).toBe("waiting_input");
  });

  it("런 상세로 그릴 때도 프로세스가 죽어 running으로 남은 노드 줄을 닫는다", () => {
    const { waiting } = wfContract.agent;
    const crashed: typeof waiting = {
      ...waiting,
      status: "failed",
      ended_at: "2026-11-03T00:00:09Z",
      pending_interaction: null,
      error: { code: "interrupted", message: "서버가 다시 시작해 런을 멈췄다", retryable: false },
    };
    const nodeRuns = waiting.node_runs.map((item) =>
      item.node_id === "agent_1" ? { ...item, status: "running" as const, ended_at: null } : item,
    );
    const state = stateFromRun(crashed, nodeRuns);
    expect(latest(state, "agent_1")).toMatchObject({
      status: "cancelled",
      endedAt: "2026-11-03T00:00:09Z",
    });
    expect(latest(state, "start").status).toBe("succeeded");

    // 입력을 기다리다 실패한 런이면 기다리던 노드 줄은 런의 오류로 닫는다.
    const timedOut = stateFromRun(
      {
        ...crashed,
        error: { code: "input_timeout", message: "입력 대기 시간이 지났다", retryable: false },
      },
      waiting.node_runs,
    );
    expect(latest(timedOut, "agent_1")).toMatchObject({
      status: "failed",
      error: { code: "input_timeout", message: "입력 대기 시간이 지났다" },
    });
    // 기다리는 중인 런은 그대로 둔다.
    expect(latest(stateFromRun(waiting, waiting.node_runs), "agent_1").status).toBe(
      "waiting_input",
    );
  });
});

describe("wfRunStore 인용과 일일 한도(3단계 12.4)", () => {
  const resource = {
    position: 1,
    dataset_id: "kb-1",
    dataset_name: "사내 규정",
    document_id: "doc-1",
    document_name: "휴가 규정.md",
    data_source_type: "upload_file",
    segment_id: "seg-1",
    segment_position: 3,
    score: 0.82,
    content: "연차는 다음 해로 5일까지 넘길 수 있다.",
  };

  it("workflow.completed의 retriever_resources를 런 상태에 둔다", () => {
    const state = run([
      wfEvent(1, "workflow.completed", {
        status: "succeeded",
        usage_total: {},
        node_runs: 4,
        duration_ms: 10,
        retriever_resources: [resource],
      }),
    ]);
    expect(state.retrieverResources).toEqual([resource]);
  });

  it("retry_after가 있으면 다시 할 수 있는 시각을 오류에 붙인다", () => {
    const state = run([
      wfEvent(1, "workflow.error", {
        code: "model_unavailable",
        message: "모든 모델이 일일 한도에 걸렸다",
        retryable: true,
        retry_after: 3600,
      }),
    ]);
    expect(state.error).toMatchObject({
      code: "model_unavailable",
      retryAt: "2026-11-03T01:00:01.000Z",
    });
    const plain = run([wfEvent(1, "workflow.error", { code: "x", message: "y" })]);
    expect(plain.error?.retryAt).toBeNull();
  });
});
