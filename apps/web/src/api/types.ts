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

export const APPROVAL_MODES: readonly ApprovalMode[] = ["all_shell", "http_writes", "off"];
export const REASONING_EFFORTS: readonly ReasoningEffort[] = ["minimal", "low", "medium", "high"];
export const TOOL_GROUPS: readonly ToolGroup[] = ["shell", "files", "http", "ask_user"];
