from enum import StrEnum


class ApprovalMode(StrEnum):
    OFF = "off"
    HTTP_WRITES = "http_writes"
    ALL_SHELL = "all_shell"


class AgentVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class SkillScope(StrEnum):
    LIBRARY = "library"
    EMBEDDED = "embedded"


class SessionSource(StrEnum):
    PREVIEW = "preview"
    WEBAPP = "webapp"
    API = "api"
    TASK = "task"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM_SUMMARY = "system_summary"


class RunTrigger(StrEnum):
    CHAT = "chat"
    TASK = "task"
    RESUME = "resume"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionFileKind(StrEnum):
    UPLOAD = "upload"
    OUTPUT = "output"


class WorkspaceBackend(StrEnum):
    LOCAL = "local"
    E2B = "e2b"


class WorkspaceInstanceStatus(StrEnum):
    READY = "ready"
    BUSY = "busy"
    ARCHIVED = "archived"


class SnapshotKind(StrEnum):
    PUBLISHED = "published"


class WorkflowKind(StrEnum):
    WORKFLOW = "workflow"
    CHATFLOW = "chatflow"


class WfRunTrigger(StrEnum):
    PREVIEW = "preview"
    WEBAPP = "webapp"
    API = "api"
    SINGLE_NODE = "single_node"
    RESUME = "resume"


class WfNodeRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    WAITING_INPUT = "waiting_input"


class WfFileKind(StrEnum):
    UPLOAD = "upload"
    OUTPUT = "output"


class WfConversationSource(StrEnum):
    PREVIEW = "preview"
    WEBAPP = "webapp"
    API = "api"
