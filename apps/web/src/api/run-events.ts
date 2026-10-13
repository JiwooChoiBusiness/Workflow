/*
 * 런 이벤트(설계 4.5)와 런 입력(6.2 POST /runs/{rid}/input)의 타입. 이벤트별 data 모양은
 * packages/event-schema에서 만든 run-events.gen.ts가 단일 출처이고, 여기에는 SSE data 줄의 겉모양과
 * 화면 코드가 쓰는 이름만 둔다.
 */
import {
  RUN_EVENT_TYPES,
  TERMINAL_EVENT_TYPE,
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

export type RunEventType = Exclude<keyof RunEventDataMap, "ping">;

// 모든 이벤트의 data 줄은 {run_id, session_id, seq, ts, type, data}다(설계 12.2 M6 이후 확정).
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

export type InteractionInput =
  | { answer: string }
  | { decision: "approve"; session_auto_approve?: boolean }
  | { decision: "reject"; note?: string | null };

// 런은 언제나 run.completed로 끝나고 서버가 그 뒤에 SSE를 닫는다. run.error 뒤에도 run.completed(failed)가 온다.
export const RUN_END_EVENT = TERMINAL_EVENT_TYPE;

const KNOWN_TYPES = new Set<string>(RUN_EVENT_TYPES);

export function isKnownEventType(type: string): type is RunEvent["type"] {
  return KNOWN_TYPES.has(type);
}
