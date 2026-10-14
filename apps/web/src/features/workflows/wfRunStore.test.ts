import { describe, expect, it } from "vitest";
import type { WfEvent } from "@/api/run-events";
import { wfContract } from "@/test/wfContract";
import {
  applyWfEvent,
  applyWfEvents,
  createWfRunStore,
  finishedNodeCount,
  initialWfRunState,
  mergeNodeRuns,
  NODE_TEXT_LIMIT,
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
    expect(waiting.pendingInteraction?.interaction.kind).toBe("approval");
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
