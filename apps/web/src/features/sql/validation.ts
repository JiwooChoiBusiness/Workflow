/*
 * SQL 검사·실행 오류 코드를 사용자 문장으로 바꾼다. 코드는 서버 app/knowledge/sql/validate.py(검사 거부),
 * runner.py(실행 실패), app/services/sql_sources.py·sql_runtime.py(데이터 소스 상태)의 것이다(설계 10.4,
 * 17.4 "M27 이후 확정"). 서버 문장은 어느 표·열·함수인지 담고 있어 바꾼 문장 아래에 그대로 덧붙인다.
 */
import type { TFunction } from "i18next";
import { ApiError } from "@/api/http";
import { errorMessage } from "@/lib/errors";
import type { T } from "@/lib/formSchemas";

export type ST = TFunction<"sql">;

// 서버 TableName(app/schemas/sql_source.py): 질의 파일의 표 이름이 그대로 SQL 식별자가 된다.
export const TABLE_NAME = /^[a-z][a-z0-9_]{0,62}$/;

// 검사(sqlglot)가 거부하는 코드. stage=validate로 온다.
export const REJECT_CODES = [
  "sql_empty",
  "sql_too_long",
  "sql_invalid",
  "multiple_statements",
  "not_select",
  "cte_name_conflict",
  "table_not_allowed",
  "function_not_allowed",
  "column_hidden",
  "column_unresolved",
  "limit_invalid",
] as const;

// 샌드박스 실행이 내는 코드. stage=execute로 온다.
export const RUN_CODES = [
  "sql_error",
  "sql_timeout",
  "sql_memory_limit",
  "sql_runtime_missing",
  "sql_failed",
] as const;

// 노드·도구가 데이터 소스를 쓰지 못할 때와 승인 거절.
export const SOURCE_CODES = [
  "sql_rejected",
  "sql_source_not_prepared",
  "sql_source_not_found",
  "sql_source_required",
  "sql_source_file_missing",
  "sql_unavailable",
  "sql_runtime_install_failed",
  "sql_runtime_install_timeout",
  "sql_prepare_failed",
] as const;

export type SqlCode =
  (typeof REJECT_CODES)[number] | (typeof RUN_CODES)[number] | (typeof SOURCE_CODES)[number];

const KNOWN: ReadonlySet<string> = new Set<string>([
  ...REJECT_CODES,
  ...RUN_CODES,
  ...SOURCE_CODES,
]);

export function isSqlCode(code: string): code is SqlCode {
  return KNOWN.has(code);
}

/*
 * 노드 실패를 멈춘 곳으로 나눈다. validate는 검사 거부, rejected는 승인 카드에서 거절, source는 데이터 소스·
 * 런타임이 준비되지 않아 SQL을 실행하지 않은 것, 나머지(execute)는 실행하다 실패한 것이다. 코드만 보고 나누므로
 * 실행 환경이 없을 때의 sql_failed는 execute에 든다.
 */
export type FailureKind = "validate" | "rejected" | "source" | "execute";

const VALIDATE_SET: ReadonlySet<string> = new Set(REJECT_CODES);
const STATE_SET: ReadonlySet<string> = new Set<string>([
  ...SOURCE_CODES.filter((code) => code !== "sql_rejected"),
  "sql_runtime_missing",
]);

export function failureKind(code: string): FailureKind {
  if (VALIDATE_SET.has(code)) return "validate";
  if (code === "sql_rejected") return "rejected";
  if (STATE_SET.has(code)) return "source";
  return "execute";
}

// 코드의 사용자 문장. 모르는 코드면 null이라 부르는 쪽이 서버 문장을 그대로 쓴다.
export function sqlCodeText(code: string, t: ST): string | null {
  return isSqlCode(code) ? t(`reject.${code}`) : null;
}

export interface FailureText {
  // 바꾼 문장(모르는 코드면 서버 문장)
  title: string;
  // 서버 문장. 제목과 같으면 비운다.
  detail: string | null;
}

// SqlFailure·노드 오류처럼 {code, message}인 것. 노드 출력 error는 "<코드>: <문장>" 글이라 그것도 읽는다.
export function failureText(failure: { code: string; message: string }, t: ST): FailureText {
  const title = sqlCodeText(failure.code, t);
  if (title === null) return { title: failure.message || failure.code, detail: null };
  const detail = failure.message.trim();
  return { title, detail: detail && detail !== title ? detail : null };
}

// sql_query 노드의 error 출력("<코드>: <문장>")을 나눈다. 성공이면 빈 글이다.
export function splitNodeError(text: string): { code: string; message: string } | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const match = /^([a-z_]+):\s*([\s\S]*)$/.exec(trimmed);
  return match ? { code: match[1]!, message: match[2] ?? "" } : { code: "", message: trimmed };
}

function detailsOf(error: ApiError): Record<string, unknown> {
  return typeof error.details === "object" &&
    error.details !== null &&
    !Array.isArray(error.details)
    ? (error.details as Record<string, unknown>)
    : {};
}

function duplicateNames(error: ApiError): string {
  const items = detailsOf(error).duplicates;
  if (!Array.isArray(items)) return "";
  return items
    .map((item: unknown) => {
      const record = (typeof item === "object" && item !== null ? item : {}) as Record<
        string,
        unknown
      >;
      const name = record.document_name ?? record.filename;
      return typeof name === "string" ? name : "";
    })
    .filter(Boolean)
    .join(", ");
}

function firstProblem(error: ApiError): string | null {
  const first = Array.isArray(error.details) ? (error.details[0] as { type?: unknown }) : null;
  return typeof first?.type === "string" ? first.type : null;
}

export interface SqlErrorOptions {
  // sql_prepare_failed의 details.file_id를 파일 이름으로 바꾼다.
  fileName?: (fileId: string) => string | undefined;
}

// 데이터 소스 화면의 API 오류를 문장으로. 문장이 없는 코드는 공용 errorMessage를 쓴다.
export function sqlErrorMessage(
  error: unknown,
  t: ST,
  tc: T,
  options: SqlErrorOptions = {},
): string {
  if (!(error instanceof ApiError)) return errorMessage(error, tc);
  switch (error.code) {
    case "file_too_large": {
      const bytes = detailsOf(error).max_bytes;
      const mb = typeof bytes === "number" ? Math.floor(bytes / (1024 * 1024)) : 200;
      return t("errors.fileTooLarge", { mb });
    }
    case "unsupported_file_type":
    case "multipart_required":
      return t("errors.unsupportedType");
    case "too_many_files":
      return t("errors.tooManyFiles");
    case "sql_source_empty":
      return t("errors.sourceEmpty");
    case "sql_prepare_failed": {
      const fileId = detailsOf(error).file_id;
      const name = typeof fileId === "string" ? options.fileName?.(fileId) : undefined;
      const base = name
        ? t("errors.prepareFailedFile", { file: name })
        : t("reject.sql_prepare_failed");
      return error.message ? `${base} ${error.message}` : base;
    }
    case "kb_not_qa":
      return t("errors.kbNotQa");
    case "duplicate_document":
      return t("errors.duplicateExample", { names: duplicateNames(error) || "-" });
    case "paid_key_missing":
      return t("errors.paidKeyMissing");
    case "slug_taken":
      return t("errors.slugTaken");
    case "sql_unavailable":
    case "storage_unavailable":
      return t("errors.unavailable");
    case "validation_error": {
      const type = firstProblem(error);
      if (type === "table_name_taken") return t("errors.tableNameTaken");
      if (type === "data_policy_not_acknowledged") return t("errors.policyRequired");
      return errorMessage(error, tc);
    }
    default: {
      const text = sqlCodeText(error.code, t);
      return text ?? errorMessage(error, tc);
    }
  }
}
