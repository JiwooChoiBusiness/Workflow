import type { DataRunEvent, RunEvent, RunEventOf, RunEventType } from "@/api/run-events";
import approvalApproved from "./fixtures/run-events/approval-approved.json";
import approvalRejected from "./fixtures/run-events/approval-rejected.json";
import approvalWait from "./fixtures/run-events/approval-wait.json";
import edgeCases from "./fixtures/run-events/edge-cases.json";
import questionAnswered from "./fixtures/run-events/question-answered.json";
import questionWait from "./fixtures/run-events/question-wait.json";
import textOnly from "./fixtures/run-events/text-only.json";

export const fixtures = {
  textOnly: textOnly as unknown as RunEvent[],
  approvalWait: approvalWait as unknown as RunEvent[],
  approvalApproved: approvalApproved as unknown as RunEvent[],
  approvalRejected: approvalRejected as unknown as RunEvent[],
  questionWait: questionWait as unknown as RunEvent[],
  questionAnswered: questionAnswered as unknown as RunEvent[],
  edgeCases: edgeCases as unknown as RunEvent[],
};

// 테스트에서 이벤트를 짧게 만든다. seq는 런별로 1부터 센다.
export function eventFactory(runId = "run-1", sessionId = "session-1") {
  let seq = 0;
  return <T extends RunEventType>(type: T, data: RunEventOf<T>["data"]): DataRunEvent => {
    seq += 1;
    return {
      run_id: runId,
      session_id: sessionId,
      seq,
      ts: new Date(Date.UTC(2026, 9, 13, 5, 0, seq)).toISOString(),
      type,
      data,
    } as DataRunEvent;
  };
}
