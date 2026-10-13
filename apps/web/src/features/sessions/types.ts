import type { components } from "@/api/schema";

type Schemas = components["schemas"];

export type SessionOut = Schemas["SessionOut"];
export type SessionDetail = Schemas["SessionDetail"];
export type SessionPatch = Schemas["SessionPatch"];
export type MessageOut = Schemas["MessageOut"];
export type MessageList = Schemas["MessageList"];
export type SessionFileOut = Schemas["SessionFileOut"];
export type RunOut = Schemas["RunOut"];
export type PendingInteractionOut = Schemas["PendingInteractionOut"];
export type WorkspaceStatusOut = Schemas["WorkspaceStatusOut"];

// POST /runs/{rid}/input 본문. 화면은 response_mode=none(202)으로 보내고 이벤트는 따로 다시 받는다.
export type RunInputBody = Pick<Schemas["RunInput"], "answer" | "decision" | "note"> & {
  session_auto_approve?: boolean;
};
