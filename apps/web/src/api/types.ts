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
// 워크스페이스 전체 런 목록(GET /wf-runs)의 항목. 지운 워크플로우의 런도 이름과 함께 온다.
export type WfRunLogItem = Schemas["WfRunLogItem"];
export type WfRunLogList = Schemas["WfRunLogList"];
export type WfNodeRunOut = Schemas["WfNodeRunOut"];
export type WfNodeRunDetail = Schemas["WfNodeRunDetail"];
export type WfPendingOut = Schemas["WfPendingOut"];
export type RunInput = Schemas["RunInput"];
export type WfRunInput = Schemas["WfRunInput"];
// tool 노드의 내장 도구(GET /node-types의 tools, M19)
export type ToolSpec = Schemas["ToolSpecOut"];
export type ToolParam = ToolSpec["params"][number];
// 웹앱의 워크플로우(설계 6.2)
export type FlowAppOut = Schemas["FlowAppOut"];
export type FlowField = Schemas["FlowField"];
export type FlowRunResult = Schemas["FlowRunResult"];
// 챗플로우 대화(설계 6.1, 6.2). 미리보기·웹앱·API 대화가 같은 모양이다.
export type WfConversationOut = Schemas["WfConversationOut"];
export type WfConversationDetail = Schemas["WfConversationDetail"];
export type WfConversationList = Schemas["WfConversationList"];
export type WfConversationSource = WfConversationOut["source"];
export type WfMessageOut = Schemas["WfMessageOut"];
export type WfMessageList = Schemas["WfMessageList"];
export type WfMessageCreate = Schemas["WfMessageCreate"];
export type ConversationVariableOut = Schemas["ConversationVariableOut"];

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

// 3단계 지식 베이스(설계 12.1). 설정 JSON(규칙·검색 설정·색인 설정·데이터 정책)도 서버 모양 그대로다.
export type KnowledgeBaseOut = Schemas["KnowledgeBaseOut"];
export type KnowledgeBaseCreate = Schemas["KnowledgeBaseCreate"];
export type KnowledgeBasePatch = Schemas["KnowledgeBasePatch"];
export type KnowledgeBaseStats = Schemas["KnowledgeBaseStats"];
export type KnowledgeDefaults = Schemas["KnowledgeDefaults"];
export type EmbeddingDailyUsage = Schemas["EmbeddingDailyUsage"];
export type EmbeddingLimits = Schemas["EmbeddingLimits"];
export type EmbeddingLimiterInfo = Schemas["EmbeddingLimiterInfo"];
export type EmbeddingSettingsIn = Schemas["EmbeddingSettingsIn"];
export type ProcessRule = Schemas["ProcessRule"];
export type RetrievalSettings = Schemas["RetrievalSettings"];
export type DocForm = KnowledgeBaseOut["doc_form"];
export type IndexingTechnique = KnowledgeBaseOut["indexing_technique"];
export type KeyTier = Schemas["DataPolicy"]["key_tier"];
export type SearchMethod = NonNullable<RetrievalSettings["search_method"]>;
export type RerankingMode = NonNullable<RetrievalSettings["reranking_mode"]>;
export type KbDocumentOut = Schemas["KbDocumentOut"];
export type KbDocumentList = Schemas["KbDocumentList"];
export type KbDocumentsCreated = Schemas["KbDocumentsCreated"];
export type DisplayStatus = KbDocumentOut["display_status"];
export type IndexingStatus = KbDocumentOut["indexing_status"];
export type DocumentStatusResult = Schemas["DocumentStatusResult"];
export type DocumentStatusSkip = Schemas["DocumentStatusSkip"];
export type DocumentPreview = Schemas["DocumentPreview"];
export type DocumentMetadataValue = Schemas["DocumentMetadataValue"];
export type DocumentMetadataUpdate = Schemas["DocumentMetadataUpdate"];
export type DatasetIndexingStatus = Schemas["DatasetIndexingStatus"];
export type SegmentOut = Schemas["SegmentOut"];
export type SegmentList = Schemas["SegmentList"];
export type SegmentPatch = Schemas["SegmentPatch"];
export type ChildChunkOut = Schemas["ChildChunkOut"];
export type MetadataFieldOut = Schemas["MetadataFieldOut"];
export type MetadataFieldList = Schemas["MetadataFieldList"];
export type MetadataType = MetadataFieldOut["type"];
export type RetrieveRequest = Schemas["RetrieveRequest"];
export type RetrieveResponse = Schemas["RetrieveResponse"];
export type RetrieveRecord = Schemas["RetrieveRecord"];
export type RetrievalWarning = Schemas["RetrievalWarning"];
export type FusionDetail = Schemas["FusionDetail"];
export type QueryLogOut = Schemas["QueryLogOut"];
export type QueryLogList = Schemas["QueryLogList"];
export type EvalCaseOut = Schemas["EvalCaseOut"];
export type EvalCaseCreate = Schemas["EvalCaseCreate"];
export type EvalImportResult = Schemas["EvalImportResult"];
export type EvalRunOut = Schemas["EvalRunOut"];
export type EvalRunSummary = Schemas["EvalRunSummary"];
export type EvalMetrics = Schemas["EvalMetrics"];
export type EvalCaseResult = Schemas["EvalCaseResult"];
// 지식 노드 폼의 선택지(GET /node-types의 kb_options), 인용, 에이전트의 지식 베이스(설계 8.1~8.4)
export type KnowledgeNodeOptions = Schemas["KnowledgeNodeOptions"];
export type RetrieverResource = Schemas["RetrieverResourceData"];
export type AgentKnowledge = Schemas["AgentKnowledge"];
export type MetadataFilterRequest = NonNullable<RetrieveRequest["metadata_filter"]>;
export type MetadataCondition = Schemas["MetadataCondition"];
export type MetadataOperator = MetadataCondition["comparison_operator"];

// 3단계 Text2SQL 데이터 소스(설계 10장, 12.1). 스키마 스냅샷·주석·한도도 서버 모양 그대로다.
export type SqlSourceOut = Schemas["SqlSourceOut"];
export type SqlSourceSummary = Schemas["SqlSourceSummary"];
export type SqlSourceCreate = Schemas["SqlSourceCreate"];
export type SqlSourcePatch = Schemas["SqlSourcePatch"];
export type SqlSourceFileOut = Schemas["SqlSourceFileOut"];
export type SqlFileOptions = Schemas["SqlFileOptions"];
export type SqlFilePatch = Schemas["SqlFilePatch"];
export type SqlLimits = Schemas["SqlLimits"];
export type SqlTable = Schemas["SqlTable"];
export type SqlSchemaWarning = Schemas["SqlSchemaWarning"];
export type SqlTableNote = Schemas["TableNote"];
export type SqlColumnNote = Schemas["ColumnNote"];
export type SqlAnnotationsUpdate = Schemas["SqlAnnotationsUpdate"];
export type SqlQueryResult = Schemas["SqlQueryResult"];
export type SqlFailure = Schemas["SqlFailure"];
export type SqlQueryLogOut = Schemas["SqlQueryLogOut"];
export type SqlRuntimeStatus = Schemas["SqlRuntimeStatus"];
export type SqlExportSchemaOut = Schemas["SqlExportSchemaOut"];
export type SqlExampleCreate = Schemas["SqlExampleCreate"];
export type SqlNodeOptions = Schemas["SqlNodeOptions"];
export type SqlSourceKind = SqlSourceOut["kind"];
export type SqlSourceState = SqlSourceOut["status"]["state"];
export type SqlApproval = SqlSourceOut["approval"];
export type SqlEncoding = SqlFileOptions["encoding"];
