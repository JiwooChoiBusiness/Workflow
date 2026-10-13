import type { components } from "@/api/schema";

// 로그 화면이 쓰는 서버 타입. 공용 src/api/types.ts와 이름이 겹치지 않게 여기에 둔다.
type Schemas = components["schemas"];

export type SessionOut = Schemas["SessionOut"];
export type SessionDetail = Schemas["SessionDetail"];
export type MessageOut = Schemas["MessageOut"];
export type MessageList = Schemas["MessageList"];
export type RunOut = Schemas["RunOut"];
export type RunLogItem = Schemas["RunLogItem"];
export type RunLogList = Schemas["RunLogList"];
export type RunEventOut = Schemas["RunEventOut"];
export type SessionFileOut = Schemas["SessionFileOut"];
export type SessionSource = SessionOut["source"];
export type RunStatus = RunOut["status"];

export const SESSION_SOURCES: readonly SessionSource[] = ["preview", "webapp", "api", "task"];
export const RUN_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "waiting_input",
  "succeeded",
  "failed",
  "cancelled",
];
// 아직 끝나지 않은 런. 화면이 몇 초마다 다시 읽는다.
export const ACTIVE_RUN_STATUSES: readonly RunStatus[] = ["queued", "running"];

export function isRunActive(status: RunStatus | null | undefined): boolean {
  return status !== null && status !== undefined && ACTIVE_RUN_STATUSES.includes(status);
}
