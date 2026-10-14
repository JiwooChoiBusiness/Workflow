/*
 * 런 이벤트(설계 4.5, 2단계 3.9)와 런 입력(6.2 POST /runs/{rid}/input)의 타입. 이벤트별 data 모양은
 * packages/event-schema에서 만든 run-events.gen.ts가 단일 출처이고, 여기에는 SSE data 줄의 겉모양과
 * 화면 코드가 쓰는 이름만 둔다.
 */
import {
  RUN_EVENT_TYPES,
  TERMINAL_EVENT_TYPES,
  type RunCompletedData,
  type RunEventDataMap,
  type RunWaitingInputData,
  type ToolCallCompletedData,
} from "./run-events.gen";

export type { MessageFileData, RunErrorData, UsageTotal } from "./run-events.gen";

export type ToolCallResult = ToolCallCompletedData["status"];
export type InteractionKind = RunWaitingInputData["kind"];
export type RunTerminalStatus = RunCompletedData["status"];
export type WaitingInputData = RunWaitingInputData;

// 워크플로우 런의 이벤트(스키마 v2). 에이전트 런 화면(runStore 등)은 다루지 않는다.
type WfEventPrefix = "workflow." | "node." | "iteration." | "answer.";
export type WfEventType = Extract<keyof RunEventDataMap, `${WfEventPrefix}${string}`>;

// 에이전트 런의 이벤트.
export type RunEventType = Exclude<keyof RunEventDataMap, "ping" | WfEventType>;

// 에이전트 런 이벤트의 data 줄은 {run_id, session_id, seq, ts, type, data}다(설계 12.2 M6 이후 확정).
export type RunEventOf<T extends RunEventType> = {
  run_id: string;
  session_id: string;
  seq: number;
  ts: string;
  type: T;
  data: RunEventDataMap[T];
};

export type DataRunEvent = { [T in RunEventType]: RunEventOf<T> }[RunEventType];

// ping은 런에 속하지 않는 생존 신호라 seq가 없다(6.5).
export interface PingEvent {
  type: "ping";
  run_id?: string;
  session_id?: string;
  seq?: number;
  ts?: string;
  data?: RunEventDataMap["ping"];
}

export type RunEvent = DataRunEvent | PingEvent;

// 워크플로우 런 이벤트는 세션이 없어 session_id가 null이고 챗플로우만 conversation_id가 있다.
export type WfEventOf<T extends WfEventType> = {
  run_id: string;
  session_id: null;
  conversation_id?: string | null;
  seq: number;
  ts: string;
  type: T;
  data: RunEventDataMap[T];
};

export type WfEvent = { [T in WfEventType]: WfEventOf<T> }[WfEventType];

export type InteractionInput =
  | { answer: string }
  | { decision: "approve"; session_auto_approve?: boolean }
  | { decision: "reject"; note?: string | null };

// 런은 언제나 완료 이벤트(에이전트 런 run.completed, 워크플로우 런 workflow.completed)로 끝나고 서버가 그
// 뒤에 SSE를 닫는다. run.error·workflow.error 뒤에도 완료 이벤트(failed)가 온다.
const TERMINAL_TYPES = new Set<string>(TERMINAL_EVENT_TYPES);

export function isTerminalEventType(type: string): boolean {
  return TERMINAL_TYPES.has(type);
}

const KNOWN_TYPES = new Set<string>(RUN_EVENT_TYPES);

export function isKnownEventType(type: string): type is keyof RunEventDataMap {
  return KNOWN_TYPES.has(type);
}
