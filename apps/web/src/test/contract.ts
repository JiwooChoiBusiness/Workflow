/*
 * 실제 서버(LLM_FAKE=true, 2026-10-13)에서 받은 응답과 이벤트. 손으로 만든 픽스처(fixtures/run-events)와 달리
 * 서버 계약 그대로라 복구·재접속 테스트의 기준으로 쓴다. 가짜 어댑터의 기본 각본은 shell 한 번 뒤 텍스트이고,
 * question은 각본 파일(write_file → ask_user → 텍스트)로 받았다.
 */
import type { DataRunEvent } from "@/api/run-events";
import type {
  MessageList,
  RunOut,
  SessionDetail,
  SessionFileOut,
  SessionOut,
} from "@/features/sessions/types";
import approval from "./fixtures/contract/approval.json";
import autoApprove from "./fixtures/contract/auto-approve.json";
import conflict from "./fixtures/contract/conflict.json";
import question from "./fixtures/contract/question.json";
import stopWaiting from "./fixtures/contract/stop-waiting.json";

export interface ContractFixtures {
  approval: {
    send: { runId: string; sessionId: string; events: DataRunEvent[] };
    detailWaiting: SessionDetail;
    messagesWaiting: MessageList;
    input: RunOut;
    // POST /input 202 뒤 GET events?after=<마지막 seq>&run_id=로 받은 이벤트
    resume: DataRunEvent[];
    messagesDone: MessageList;
    detailDone: SessionDetail;
  };
  autoApprove: {
    send: DataRunEvent[];
    resume: DataRunEvent[];
    detail: SessionDetail;
    autoRun: DataRunEvent[];
    patchOff: SessionOut;
  };
  stopWaiting: { send: DataRunEvent[]; stop: RunOut; after: DataRunEvent[] };
  question: {
    upload: SessionFileOut;
    send: DataRunEvent[];
    detail: SessionDetail;
    messagesWaiting: MessageList;
    // 질문에 새 메시지로 답했을 때의 응답 스트림. 같은 런이 이어진다.
    answer: DataRunEvent[];
    messagesDone: MessageList;
  };
  conflict: { status: number; body: { error: { code: string; message: string } } };
}

export const contract = {
  approval,
  autoApprove,
  stopWaiting,
  question,
  conflict,
} as unknown as ContractFixtures;

// 서버가 보내는 SSE 본문(설계 6.5)
export function sseBody(events: readonly DataRunEvent[]): string {
  return events
    .map((event) => `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
    .join("");
}
