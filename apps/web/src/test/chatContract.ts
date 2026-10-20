/*
 * 8501 시험 서버(LLM_FAKE=1, 2026-10-14)의 콘솔 대화 API로 받은 챗플로우 대화. basic은 chat_basic
 * (start → llm(메모리) → answer)의 미리보기 대화 두 턴이고, approval은 start → llm → answer_1 → agent(셸
 * 승인) → answer_2에서 승인을 기다린 뒤 같은 대화에 또 보내 409를 받고, 승인해 재개한 것이다.
 */
import type { WfEvent } from "@/api/run-events";
import type { WfConversationDetail, WfMessageList, WfRunOut } from "@/api/types";
import chatflow from "./fixtures/contract/chatflow.json";

export interface ChatTurnContract {
  text: string;
  // x-run-id, x-conversation-id, x-message-id
  headers: Record<string, string>;
  events: WfEvent[];
}

export interface ChatContractFixtures {
  basic: {
    workflowId: string;
    conversationId: string;
    // 초안 저장(PUT draft) 응답의 answer 노드 ui
    answerUi: { prestream_sources: string[] };
    turns: ChatTurnContract[];
    messages: WfMessageList;
    detail: WfConversationDetail;
  };
  approval: {
    workflowId: string;
    conversationId: string;
    runId: string;
    // 메시지 전송 스트림. workflow.completed(waiting_input)로 닫힌다.
    events: WfEvent[];
    waitingMessages: WfMessageList;
    waitingDetail: WfConversationDetail;
    // 기다리는 동안 같은 대화에 보낸 메시지
    conflict: {
      status: number;
      body: { error: { code: string; message: string; details: Record<string, unknown> } };
    };
    // 기다리는 동안 GET /wf-runs/{rid}/events?after=0(링버퍼가 살아 있어 델타도 온다)
    replayWaiting: WfEvent[];
    input: WfRunOut;
    // 승인 뒤 GET events?after=<마지막 seq>. 멈추기 전 글은 다시 오지 않는다.
    resume: WfEvent[];
    doneMessages: WfMessageList;
    doneDetail: WfConversationDetail;
  };
}

export const chatContract = chatflow as unknown as ChatContractFixtures;
