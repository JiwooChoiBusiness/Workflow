// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { applyWfEvents, initialWfRunState } from "@/features/workflows/wfRunStore";
import { secondNodesContract } from "@/test/secondNodesContract";
import { wfContract } from "@/test/wfContract";
import { NodeTimeline } from "./NodeTimeline";
import { singleRunInputs } from "./SingleRunDialog";

afterEach(cleanup);

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const noInfo = () => undefined;

describe("NodeTimeline", () => {
  it("iteration 안쪽 노드 런을 반복 순번별로 접어 둔다", async () => {
    const user = userEvent.setup();
    const state = applyWfEvents(initialWfRunState(), wfContract.iteration.events);
    render(wrap(<NodeTimeline state={state} nodeInfo={noInfo} fetchDetail={false} />));

    const timeline = screen.getByRole("list", { name: "노드" });
    expect(
      within(timeline)
        .getAllByTestId(/^node-run-/)
        .map((item) => item.dataset.testid),
    ).toEqual(["node-run-start", "node-run-code_1", "node-run-iteration_1", "node-run-end_1"]);

    const iteration = screen.getByTestId("node-run-iteration_1");
    await user.click(within(iteration).getAllByRole("button")[0]!);
    expect(within(iteration).getByText("3/3")).toBeTruthy();
    const second = within(iteration).getByRole("button", { name: /반복 2/ });
    expect(screen.queryAllByTestId("node-run-template_3")).toHaveLength(0);
    await user.click(second);
    const inner = screen.getAllByTestId("node-run-template_3");
    expect(inner).toHaveLength(1);
    expect(inner[0]!.dataset.status).toBe("succeeded");
  });

  it("loop 안쪽 노드 런도 회차별로 접고, 돈 횟수를 최대 반복 수와 함께 보인다", async () => {
    const user = userEvent.setup();
    const state = applyWfEvents(initialWfRunState(), secondNodesContract.events);
    render(wrap(<NodeTimeline state={state} nodeInfo={noInfo} fetchDetail={false} />));

    const timeline = screen.getByRole("list", { name: "노드" });
    expect(
      within(timeline)
        .getAllByTestId(/^node-run-/)
        .map((item) => item.dataset.testid),
    ).toEqual([
      "node-run-start",
      "node-run-parse",
      "node-run-pick",
      "node-run-now",
      "node-run-loop_1",
      "node-run-ask",
    ]);
    const loop = screen.getByTestId("node-run-loop_1");
    await user.click(within(loop).getAllByRole("button")[0]!);
    expect(within(loop).getByText("3회 · 최대 5회")).toBeTruthy();
    const rounds = within(loop).getAllByRole("button", { name: /회차/ });
    expect(rounds.map((item) => item.textContent?.match(/\d+회차/)?.[0])).toEqual([
      "1회차",
      "2회차",
      "3회차",
    ]);
    await user.click(rounds[2]!);
    expect(within(loop).getAllByTestId("node-run-set")).toHaveLength(1);
  });

  it("건너뛴 노드는 펼치지 않고, 실패한 노드는 펼치면 오류를 보인다", async () => {
    const user = userEvent.setup();
    const branch = applyWfEvents(initialWfRunState(), wfContract.branch.events);
    const { unmount } = render(
      wrap(<NodeTimeline state={branch} nodeInfo={noInfo} fetchDetail={false} />),
    );
    const skipped = screen.getByTestId("node-run-template_no");
    expect(skipped.dataset.status).toBe("skipped");
    expect(within(skipped).getByText("건너뜀")).toBeTruthy();
    expect((within(skipped).getAllByRole("button")[0] as HTMLButtonElement).disabled).toBe(true);
    unmount();

    const failed = applyWfEvents(initialWfRunState(), wfContract.failure.events);
    render(wrap(<NodeTimeline state={failed} nodeInfo={noInfo} fetchDetail={false} />));
    const code = screen.getByTestId("node-run-code_1");
    await user.click(within(code).getAllByRole("button")[0]!);
    expect(within(code).getByText("ValueError: 일부러 실패")).toBeTruthy();
    expect(within(code).getByText("code_error")).toBeTruthy();
  });

  it("건너뛴 노드를 반복 안으로 잘못 짐작했어도 그래프가 바깥이라 하면 바깥에 둔다", () => {
    const branch = applyWfEvents(initialWfRunState(), wfContract.branch.events);
    const skippedId = branch.latestByNode.template_no!;
    const state = {
      ...branch,
      nodeRuns: {
        ...branch.nodeRuns,
        [skippedId]: {
          ...branch.nodeRuns[skippedId]!,
          iterationId: "iteration_9",
          iterationIndex: 0,
        },
      },
    };
    const info = (nodeId: string) => ({ type: "template", title: nodeId, parentId: null });
    render(wrap(<NodeTimeline state={state} nodeInfo={info} fetchDetail={false} />));
    const timeline = screen.getByRole("list", { name: "노드" });
    expect(within(timeline).getByTestId("node-run-template_no").dataset.status).toBe("skipped");
  });

  it("입력을 기다리는 노드는 펼쳐 두고 자식 런 자리를 그린다", () => {
    const state = applyWfEvents(initialWfRunState(), wfContract.agent.events);
    render(
      wrap(
        <NodeTimeline
          state={state}
          nodeInfo={noInfo}
          fetchDetail={false}
          renderChild={(run) => <p>자식 {run.childSessionId}</p>}
        />,
      ),
    );
    const agent = screen.getByTestId("node-run-agent_1");
    expect(agent.dataset.status).toBe("waiting_input");
    const session = state.nodeRuns[state.latestByNode.agent_1!]!.childSessionId;
    expect(within(agent).getByText(`자식 ${session}`)).toBeTruthy();
  });
});

describe("singleRunInputs", () => {
  it("변수 타입대로 값을 바꾸고 JSON으로 읽지 못하면 글자 그대로 둔다", () => {
    const variables = [
      { selector: "start.q", scope: "start", name: "q", type: "string" as const },
      { selector: "code_1.n", scope: "code_1", name: "n", type: "number" as const },
      { selector: "llm_1.json", scope: "llm_1", name: "json", type: "object" as const },
      { selector: "x.raw", scope: "x", name: "raw", type: null },
      { selector: "start.doc", scope: "start", name: "doc", type: "file" as const },
      { selector: "sys.flag", scope: "sys", name: "flag", type: "boolean" as const },
    ];
    expect(
      singleRunInputs(variables, {
        "start.q": "",
        "code_1.n": "12",
        "llm_1.json": '{"a": [1, 2]}',
        "x.raw": "그냥 글",
        "start.doc": [{ id: "f1", filename: "a.txt", size: 3 }],
        "sys.flag": true,
      }),
    ).toEqual({
      inputs: {
        "start.q": "",
        "code_1.n": 12,
        "llm_1.json": { a: [1, 2] },
        "x.raw": "그냥 글",
        "start.doc": "f1",
        "sys.flag": true,
      },
      invalid: [],
    });
    expect(singleRunInputs(variables, { "code_1.n": "열" }).invalid).toEqual(["code_1.n"]);
  });
});
