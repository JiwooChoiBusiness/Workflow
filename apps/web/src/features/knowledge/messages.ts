/*
 * 지식 화면의 오류·경고·건너뜀 사유를 문장으로 바꾼다. 서버 코드는 app/services/knowledge*.py,
 * app/llm/embeddings.py, app/services/knowledge_search.py의 것이다(설계 17.4 "M23 이후 확정").
 */
import type { TFunction } from "i18next";
import { ApiError } from "@/api/http";
import type { DocumentStatusSkip, RetrievalSettings, RetrievalWarning } from "@/api/types";
import { errorMessage } from "@/lib/errors";
import type { T } from "@/lib/formSchemas";
import { formatDateTime } from "@/lib/format";

export type KT = TFunction<"knowledge">;

function detailsOf(error: ApiError): Record<string, unknown> {
  return typeof error.details === "object" &&
    error.details !== null &&
    !Array.isArray(error.details)
    ? (error.details as Record<string, unknown>)
    : {};
}

// 429의 Retry-After(초)를 절대 시각으로. 헤더가 없으면 본문 details.retry_after(초)를 본다.
function retryAt(error: ApiError, now: number): Date | null {
  const seconds =
    error.retryAfter ??
    (typeof detailsOf(error).retry_after === "number"
      ? (detailsOf(error).retry_after as number)
      : null);
  return seconds === null ? null : new Date(now + seconds * 1000);
}

function duplicateNames(error: ApiError): string {
  const items = detailsOf(error).duplicates;
  if (!Array.isArray(items)) return "";
  return items
    .map((item: unknown) => {
      if (typeof item !== "object" || item === null) return "";
      const record = item as Record<string, unknown>;
      const name = record.document_name ?? record.duplicate_of ?? record.filename;
      return typeof name === "string" ? name : "";
    })
    .filter(Boolean)
    .join(", ");
}

export interface MessageOptions {
  uploadMaxMb?: number;
  uploadMaxFiles?: number;
  now?: number;
}

export function knowledgeErrorMessage(
  error: unknown,
  t: KT,
  tc: T,
  options: MessageOptions = {},
): string {
  if (!(error instanceof ApiError)) return errorMessage(error, tc);
  const now = options.now ?? Date.now();
  switch (error.code) {
    case "rate_limited": {
      const seconds = error.retryAfter;
      return seconds
        ? t("errors.rateLimited", { seconds: Math.ceil(seconds) })
        : t("errors.rateLimitedNoWait");
    }
    case "rate_limited_daily": {
      const at = retryAt(error, now);
      return at
        ? t("errors.rateLimitedDaily", { time: formatDateTime(at.toISOString()) })
        : t("errors.rateLimitedDailyNoTime");
    }
    case "paid_key_missing":
      return t("errors.paidKeyMissing");
    case "model_request_invalid":
      return t("errors.modelRequestInvalid");
    case "model_auth":
      return t("errors.modelAuth");
    case "model_unavailable":
      return t("errors.modelUnavailable");
    case "index_unavailable": {
      const reason = detailsOf(error).reason;
      const base = t("errors.indexUnavailable");
      return typeof reason === "string" && reason
        ? `${base} ${t("errors.indexUnavailableReason", { reason })}`
        : base;
    }
    case "eval_import_empty":
      return t("errors.evalImportEmpty");
    case "eval_cases_empty":
      return t("errors.evalCasesEmpty");
    case "eval_cases_full":
      return t("errors.evalCasesFull");
    case "eval_expected_required":
      return t("errors.evalExpectedRequired");
    case "csv_too_large":
      return t("errors.csvTooLarge");
    case "csv_not_utf8":
      return t("errors.csvNotUtf8");
    case "duplicate_document":
      return t("errors.duplicate", { names: duplicateNames(error) || "-" });
    case "unsupported_file_type":
    case "unsupported_media_type":
      return t("errors.unsupportedType");
    case "file_too_large":
      return t("errors.fileTooLarge", { mb: options.uploadMaxMb ?? 50 });
    case "too_many_files":
      return t("errors.tooManyFiles", { count: options.uploadMaxFiles ?? 20 });
    case "text_too_large":
      return t("errors.textTooLarge");
    case "content_blank":
      return t("errors.contentBlank");
    case "document_indexing":
      return t("errors.documentIndexing");
    case "document_archived":
      return t("errors.documentArchived");
    case "reembed_confirmation_required":
      return t("errors.reembedRequired");
    case "downgrade_not_allowed":
      return t("errors.downgrade");
    case "slug_taken":
      return t("errors.slugTaken");
    case "metadata_built_in":
      return t("errors.builtIn");
    case "not_hierarchical":
      return t("errors.notHierarchical");
    case "extractor_unavailable":
      return t("errors.extractorUnavailable");
    case "model_read_pdf_only":
      return t("errors.modelReadPdfOnly");
    case "validation_error": {
      // 서비스 검사(Problems)는 type에 코드를 싣는다. 화면 문장이 있는 코드는 그것으로 바꾼다.
      const first = Array.isArray(error.details) ? (error.details[0] as { type?: unknown }) : null;
      if (first?.type === "search_method_unavailable") return t("errors.searchMethodUnavailable");
      if (first?.type === "top_k_too_large") return t("errors.topKTooLarge");
      return errorMessage(error, tc);
    }
    default:
      return errorMessage(error, tc);
  }
}

// 일일 한도처럼 오래 기다려야 하면 남은 초 대신 풀리는 시각을 보인다. receivedAt은 응답을 받은 때(ms)다.
const RETRY_SECONDS_SHOWN = 120;

export function warningMessage(warning: RetrievalWarning, t: KT, receivedAt?: number): string {
  const base = t(`warnings.${warning.code}`);
  const wait = warning.retry_after;
  if (warning.code !== "vector_unavailable" || !wait) return base;
  if (wait > RETRY_SECONDS_SHOWN && receivedAt !== undefined) {
    const time = formatDateTime(new Date(receivedAt + wait * 1000).toISOString());
    return `${base} ${t("warnings.vectorRetryAt", { time })}`;
  }
  return `${base} ${t("warnings.vectorRetry", { seconds: Math.ceil(wait) })}`;
}

const SKIP_CODES = new Set([
  "not_found",
  "not_indexing",
  "not_paused",
  "already_queued",
  "not_failed",
  "retry_after",
]);

type SkipCode =
  "not_found" | "not_indexing" | "not_paused" | "already_queued" | "not_failed" | "retry_after";

export function skipMessage(skip: DocumentStatusSkip, t: KT): string {
  if (!SKIP_CODES.has(skip.code)) return skip.message;
  const code = skip.code as SkipCode;
  if (code === "retry_after") {
    return t("documents.skipped.retry_after", {
      time: skip.retry_after ? formatDateTime(skip.retry_after) : "-",
    });
  }
  return t(`documents.skipped.${code}`);
}

export function retrievalSummary(
  settings: Pick<RetrievalSettings, "search_method" | "top_k">,
  t: KT,
): string {
  return t("retrieval.summary", {
    method: t(`retrieval.methods.${settings.search_method}`),
    topK: settings.top_k,
  });
}
