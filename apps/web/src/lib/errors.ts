import { ApiError } from "@/api/http";
import type { T } from "./formSchemas";

interface ValidationDetail {
  loc?: (string | number)[];
  msg?: string;
}

// 라우트가 아예 없을 때 서버 기본 처리기가 주는 응답(app/api/errors.py). 아직 없는 API(M6·M9)를 부른 경우다.
export function isMissingEndpoint(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    ((error.status === 404 && error.message === "Not Found") || error.status === 405)
  );
}

export function errorMessage(error: unknown, t: T): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return t("errors.network");
    if (isMissingEndpoint(error)) return t("errors.notAvailable");
    if (error.code === "validation_error" && Array.isArray(error.details) && error.details.length) {
      const first = error.details[0] as ValidationDetail;
      const field = (first.loc ?? []).filter((part) => part !== "body").join(".");
      return `${t("errors.validation")} ${field ? `${field}: ` : ""}${first.msg ?? ""}`.trim();
    }
    return error.message || t("errors.unknown");
  }
  if (error instanceof Error && error.message) return error.message;
  return t("errors.unknown");
}

export function errorDetail(error: unknown, t: T): string | null {
  if (!(error instanceof ApiError) || error.status === 0) return null;
  return t("errors.detail", { status: error.status, code: error.code });
}
