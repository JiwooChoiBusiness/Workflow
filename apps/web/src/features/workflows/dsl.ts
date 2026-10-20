/*
 * DSL 내보내기·가져오기(설계 5장). 서버 호출과 함께, 가져오기 결과·거부 사유를 화면 문장으로 바꾸는 함수와
 * 가져온 code 노드의 ui.imported_code 플래그(설계 9.3)를 다루는 함수를 둔다.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { ApiError, networkError, requestJson, toApiError } from "@/api/http";
import type { components } from "@/api/schema";
import type { WfIssue, WorkflowOut } from "@/api/types";
import { errorMessage } from "@/lib/errors";
import type { T } from "@/lib/formSchemas";
import { workflowKeys } from "./api";
import type { TW } from "./configSchemas";
import type { FlowNode } from "./graph";

type Schemas = components["schemas"];
export type WorkflowImportOut = Schemas["WorkflowImportOut"];
export type DslWarning = Schemas["DslWarning"];
export type DslSource = Schemas["DslImportSummary"]["source"];

// 서버 app/workflow/dsl/schema.py MAX_DSL_BYTES. 넘는 파일은 보내기 전에 막는다.
export const MAX_DSL_BYTES = 2 * 1024 * 1024;
const YAML_FILE = /\.ya?ml$/i;
const IMPORT_PATH = "/api/console/workflows/import";
// include_secrets로 비밀값이 평문으로 들어갔을 때 서버가 붙이는 헤더(app/api/console/workflow_dsl.py)
const WARNING_HEADER = "X-Dsl-Warning";
const SECRETS_INCLUDED = "secrets_included";
// 서버 app/workflow/nodes/code.py IMPORTED_CODE_FLAG
const IMPORTED_CODE = "imported_code";

export type DslInput = { kind: "file"; file: File } | { kind: "text"; text: string };

export type LocalDslProblem = "empty" | "extension" | "dsl_too_large";

// 서버에 보내기 전에 걸러 낼 문제. 크기는 서버처럼 UTF-8 바이트로 잰다.
export function localDslProblem(input: DslInput | null): LocalDslProblem | null {
  if (input === null) return "empty";
  if (input.kind === "file") {
    if (!YAML_FILE.test(input.file.name)) return "extension";
    if (input.file.size > MAX_DSL_BYTES) return "dsl_too_large";
    return input.file.size === 0 ? "empty" : null;
  }
  if (!input.text.trim()) return "empty";
  return new TextEncoder().encode(input.text).length > MAX_DSL_BYTES ? "dsl_too_large" : null;
}

export function importWorkflowDsl(input: DslInput, into?: string): Promise<WorkflowImportOut> {
  const query = { into };
  if (input.kind === "file") {
    const form = new FormData();
    form.append("file", input.file);
    return requestJson<WorkflowImportOut>("POST", IMPORT_PATH, { body: form, query });
  }
  return requestJson<WorkflowImportOut>("POST", IMPORT_PATH, { json: { yaml: input.text }, query });
}

// 새로 만들면 201, into로 덮어쓰면 200이다. 목록(새 워크플로우)을 다시 읽는다.
export function useImportWorkflowDsl() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ input, into }: { input: DslInput; into?: string }) =>
      importWorkflowDsl(input, into),
    onSuccess: () => client.invalidateQueries({ queryKey: [...workflowKeys.all, "list"] }),
  });
}

// Content-Disposition에서 파일 이름을 꺼낸다. 한글 이름은 filename*(RFC 5987)에 있다.
export function dslFilename(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback;
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.trim());
    } catch {
      // 깨진 퍼센트 인코딩이면 ASCII 이름을 쓴다.
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;\s]+))/i.exec(disposition);
  return plain?.[1] || plain?.[2] || fallback;
}

export interface ExportedDsl {
  blob: Blob;
  filename: string;
  secretsIncluded: boolean;
}

// version은 draft·published·버전 id. 응답 헤더(파일 이름, 비밀값 경고)가 필요해 unwrap을 쓰지 않는다.
export async function exportWorkflowDsl(
  workflow: Pick<WorkflowOut, "id" | "slug">,
  version: string,
  includeSecrets: boolean,
): Promise<ExportedDsl> {
  let result;
  try {
    result = await api.POST("/api/console/workflows/{workflow_id}/export", {
      params: {
        path: { workflow_id: workflow.id },
        query: { version, include_secrets: includeSecrets },
      },
      parseAs: "blob",
    });
  } catch (cause) {
    throw networkError(cause);
  }
  const { response } = result;
  if (!response.ok || !result.data) throw toApiError(response, result.error);
  return {
    blob: result.data,
    filename: dslFilename(response.headers.get("Content-Disposition"), `${workflow.slug}.yml`),
    secretsIncluded: response.headers.get(WARNING_HEADER) === SECRETS_INCLUDED,
  };
}

// 내보낸 YAML은 POST 응답이라 링크로 받을 수 없다. 받은 내용을 객체 URL로 내려받게 한다.
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  // 바로 해제하면 브라우저가 내려받기를 시작하기 전에 URL이 사라질 수 있다.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

const DSL_ERROR_CODES = [
  "dsl_too_large",
  "invalid_yaml",
  "unknown_dsl_format",
  "invalid_dsl",
  "unsupported_dsl_version",
  "node_version_too_new",
  "unsupported_app_mode",
  "graph_too_large",
  "kind_mismatch",
] as const;
type DslErrorCode = (typeof DSL_ERROR_CODES)[number];

function isDslErrorCode(code: string): code is DslErrorCode {
  return (DSL_ERROR_CODES as readonly string[]).includes(code);
}

export interface DslErrorText {
  title: string;
  // 서버가 알려 준 자세한 위치·값(YAML 행·열, 형식이 틀린 칸, 버전). 없으면 null.
  detail: string | null;
}

const MAX_DETAIL_LINES = 3;

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function detailLines(lines: string[], t: TW): string | null {
  if (lines.length === 0) return null;
  const shown = lines.slice(0, MAX_DETAIL_LINES);
  if (lines.length > shown.length) {
    shown.push(t("dsl.errors.more", { count: lines.length - shown.length }));
  }
  return shown.join("\n");
}

function invalidFields(details: unknown, t: TW): string | null {
  if (!Array.isArray(details)) return null;
  const lines = details.map((item) => {
    const { loc, msg } = record(item);
    const where = Array.isArray(loc) ? loc.join(".") : "";
    return where ? `${where}: ${String(msg ?? "")}` : String(msg ?? "");
  });
  return detailLines(lines, t);
}

function tooNewNodes(details: unknown, t: TW): string | null {
  if (!Array.isArray(details)) return null;
  const lines = details.map((item) => {
    const node = record(item);
    return t("dsl.errors.nodeVersion", {
      id: String(node.node_id ?? ""),
      type: String(node.type ?? ""),
      version: String(node.version ?? ""),
      latest: String(node.latest ?? ""),
    });
  });
  return detailLines(lines, t);
}

// 가져오기 거부 사유(설계 11.2 M18)를 사람 문장으로. 모르는 오류는 공통 오류 문장을 쓴다.
export function dslErrorText(error: unknown, t: TW, tc: T): DslErrorText {
  if (!(error instanceof ApiError) || !isDslErrorCode(error.code)) {
    return { title: errorMessage(error, tc), detail: null };
  }
  const title = t(`dsl.errors.${error.code}`);
  switch (error.code) {
    case "invalid_dsl":
      return { title, detail: invalidFields(error.details, t) };
    case "node_version_too_new":
      return { title, detail: tooNewNodes(error.details, t) };
    case "dsl_too_large":
    case "unknown_dsl_format":
    case "kind_mismatch":
      return { title, detail: null };
    default:
      // invalid_yaml(행·열), unsupported_dsl_version(파일의 버전), unsupported_app_mode(앱 종류),
      // graph_too_large(크기)는 서버 문장에 그 값이 있다.
      return { title, detail: error.message || null };
  }
}

export interface UnsupportedNode {
  nodeId: string;
  // 변환기가 남긴 이유(원래 노드 타입). 없으면 null.
  reason: string | null;
}

export interface ImportSummary {
  source: DslSource;
  nodes: number;
  unsupported: UnsupportedNode[];
  warnings: DslWarning[];
  errors: WfIssue[];
  issueWarnings: WfIssue[];
}

export function summarizeImport(result: WorkflowImportOut): ImportSummary {
  const { converted, issues } = result;
  const listed = new Set(converted.unsupported);
  const reasons = new Map<string, string>();
  const warnings: DslWarning[] = [];
  for (const warning of converted.warnings) {
    if (warning.code === "unsupported_node" && warning.node_id && listed.has(warning.node_id)) {
      // 옮기지 못한 노드의 경고는 그 노드 줄에 이유로 붙이고 경고 목록에는 다시 넣지 않는다.
      if (!reasons.has(warning.node_id)) reasons.set(warning.node_id, warning.message);
      continue;
    }
    warnings.push(warning);
  }
  return {
    source: converted.source,
    nodes: converted.nodes,
    unsupported: converted.unsupported.map((nodeId) => ({
      nodeId,
      reason: reasons.get(nodeId) ?? null,
    })),
    warnings,
    errors: issues.filter((issue) => issue.level === "error"),
    issueWarnings: issues.filter((issue) => issue.level !== "error"),
  };
}

// 가져온 뒤 아직 검토하지 않은 code 노드. 서버 검증과 같게 값이 정확히 true일 때만이다.
export function isImportedCode(node: Pick<FlowNode, "type" | "ui">): boolean {
  return node.type === "code" && node.ui[IMPORTED_CODE] === true;
}

// 검토 완료: 이 키만 지운다. 캔버스가 모르는 다른 ui 키는 그대로 저장돼야 한다.
export function withoutImportedCode(ui: Record<string, unknown>): Record<string, unknown> {
  const { [IMPORTED_CODE]: _flag, ...rest } = ui;
  return rest;
}
