/*
 * 8502 시험 서버(LLM_FAKE=true, 2026-10-14)의 콘솔 API로 받은 워크플로우 런 이벤트와 응답. 선형(start →
 * template → end), 분기(llm → if_else, 건너뛴 가지), llm 단일 노드 실행, iteration(code가 만든 배열 3개),
 * code 노드 실패, agent 노드 승인 대기 → 승인 → 재개(가짜 어댑터 기본 각본의 셸 한 번)를 담았다.
 */
import type { RunEventOut } from "@/features/logs/types";
import type { WfEvent } from "@/api/run-events";
import type { WfNodeRunDetail, WfNodeRunOut, WfRunDetail, WfRunOut } from "@/api/types";
import workflowRuns from "./fixtures/contract/workflow-runs.json";

export interface WfRunContract {
  runId: string;
  events: WfEvent[];
  detail: WfRunDetail;
}

export interface WfContractFixtures {
  linear: WfRunContract & {
    nodeRun: WfNodeRunDetail;
    eventLog: { items: RunEventOut[]; next_after?: number | null };
  };
  branch: WfRunContract;
  singleNode: WfRunContract;
  iteration: WfRunContract & { children: { items: WfNodeRunOut[] } };
  failure: WfRunContract;
  agent: {
    runId: string;
    events: WfEvent[];
    waiting: WfRunDetail;
    // POST /wf-runs/{rid}/input(response_mode=none)의 202 응답
    input: WfRunOut;
    // 그 뒤 GET /wf-runs/{rid}/events?after=<마지막 seq>로 받은 이벤트
    resume: WfEvent[];
    done: WfRunDetail;
  };
}

export const wfContract = workflowRuns as unknown as WfContractFixtures;
