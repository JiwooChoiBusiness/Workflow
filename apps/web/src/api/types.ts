import type { components } from "./schema";

type Schemas = components["schemas"];

export type MeOut = Schemas["MeOut"];
export type AccountOut = Schemas["AccountOut"];
export type SetupStatus = Schemas["SetupStatus"];
export type SetupRequest = Schemas["SetupRequest"];
export type LoginRequest = Schemas["LoginRequest"];
export type PasswordChangeRequest = Schemas["PasswordChangeRequest"];

export type WorkspaceOut = Schemas["WorkspaceOut"];
export type WorkspaceSettings = Schemas["WorkspaceSettings"];
export type WorkspacePatch = Schemas["WorkspacePatch"];
export type RunLimits = Schemas["RunLimits"];
export type ModelPrice = Schemas["ModelPrice"];
export type ApprovalMode = WorkspaceSettings["approval_mode_default"];

export type LlmStatus = Schemas["LlmStatus"];
export type LlmKeyStatus = Schemas["LlmKeyStatus"];
export type LlmTestResult = Schemas["LlmTestResult"];
export type LlmAttempt = Schemas["LlmAttempt"];
export type LlmError = Schemas["LlmError"];
export type Usage = Schemas["Usage"];

export type ModelList = Schemas["ModelList"];
export type ModelEntry = Schemas["ModelEntry"];
export type ModelCapabilityInfo = Schemas["ModelCapabilityInfo"];
export type ReasoningEffort = ModelCapabilityInfo["reasoning_efforts"][number];

export type AgentOut = Schemas["AgentOut"];
export type AgentCreate = Schemas["AgentCreate"];
export type AgentPatch = Schemas["AgentPatch"];
export type AgentVersionOut = Schemas["AgentVersionOut"];
export type AgentVersionSummary = Schemas["AgentVersionSummary"];
export type DraftUpdate = Schemas["DraftUpdate"];
export type EnvVarIn = Schemas["EnvVarIn"];
export type EnvVarOut = Schemas["EnvVarOut"];
export type PublishOut = Schemas["PublishOut"];
export type AgentFileOut = Schemas["AgentFileOut"];
export type ApiKeyOut = Schemas["ApiKeyOut"];
export type ApiKeyCreate = Schemas["ApiKeyCreate"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type SkillOut = Schemas["SkillOut"];
export type SkillDetailOut = Schemas["SkillDetailOut"];
export type SkillVersionOut = Schemas["SkillVersionOut"];
export type ToolGroup = NonNullable<AgentVersionOut["tools"]>[number];
export type SubAgentRef = Schemas["SubAgentRef"];
export type ResultSpec = Schemas["ResultSpec"];
export type AgentWarning = Schemas["AgentWarning"];
export type RunChildOut = Schemas["RunChildOut"];

export const APPROVAL_MODES: readonly ApprovalMode[] = ["all_shell", "http_writes", "off"];
export const REASONING_EFFORTS: readonly ReasoningEffort[] = ["minimal", "low", "medium", "high"];
export const TOOL_GROUPS: readonly ToolGroup[] = ["shell", "files", "http", "ask_user"];

// 2단계 워크플로우(설계 6.1). 그래프의 노드 config는 타입마다 모양이 달라 여기서는 dict로 둔다.
export type WorkflowOut = Schemas["WorkflowOut"];
export type WorkflowKind = WorkflowOut["kind"];
export type WorkflowCreate = Schemas["WorkflowCreate"];
export type WorkflowPatch = Schemas["WorkflowPatch"];
export type WorkflowVersionOut = Schemas["WorkflowVersionOut"];
export type WorkflowVersionSummary = Schemas["WorkflowVersionSummary"];
export type WorkflowDraftUpdate = Schemas["WorkflowDraftUpdate"];
export type WorkflowDraftCheck = Schemas["WorkflowDraftCheck"];
export type WorkflowPublishOut = Schemas["WorkflowPublishOut"];
export type WorkflowSettings = Schemas["WorkflowSettings"];
export type WorkflowLimits = Schemas["WorkflowLimits"];
export type WebappSettings = Schemas["WebappSettings"];
export type ConversationVar = Schemas["ConversationVar"];
export type VarType = ConversationVar["type"];
export type WfEnvVarOut = Schemas["WfEnvVarOut"];
export type WfIssue = Schemas["Issue"];
export type WfValidation = Schemas["ValidationOut"];
export type NodeTypeInfo = Schemas["NodeType"];
export type WfGraphOut = Schemas["Graph"];
export type WfNodeOut = Schemas["Node"];
export type WfEdgeOut = Schemas["Edge"];
export type WfFileOut = Schemas["WfFileOut"];
export type WfRunCreate = Schemas["WfRunCreate"];
export type WfNodeRunCreate = Schemas["WfNodeRunCreate"];
export type WfRunOut = Schemas["WfRunOut"];
export type WfRunDetail = Schemas["WfRunDetail"];
export type WfRunList = Schemas["WfRunList"];
export type WfNodeRunOut = Schemas["WfNodeRunOut"];
export type WfNodeRunDetail = Schemas["WfNodeRunDetail"];
export type WfPendingOut = Schemas["WfPendingOut"];
export type RunInput = Schemas["RunInput"];
// 웹앱의 워크플로우(설계 6.2)
export type FlowAppOut = Schemas["FlowAppOut"];
export type FlowField = Schemas["FlowField"];
export type FlowRunResult = Schemas["FlowRunResult"];

export const VAR_TYPES: readonly VarType[] = [
  "string",
  "number",
  "boolean",
  "object",
  "file",
  "array[string]",
  "array[number]",
  "array[boolean]",
  "array[object]",
  "array[file]",
  "array[any]",
  "any",
];
