import type { FieldPath, FieldValues, UseFormSetError } from "react-hook-form";
import { ApiError } from "@/api/http";

export interface ServerFieldError {
  loc: (string | number)[];
  msg: string;
  type?: string;
}

function isDetail(value: unknown): value is ServerFieldError {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as ServerFieldError).loc) &&
    typeof (value as ServerFieldError).msg === "string"
  );
}

// 422 validation_error의 details([{loc, msg, type}]). 요청 본문 검증과 서비스 검증이 같은 모양이다.
export function serverFieldErrors(error: unknown): ServerFieldError[] {
  if (!(error instanceof ApiError) || error.code !== "validation_error") return [];
  if (!Array.isArray(error.details)) return [];
  return error.details.filter(isDetail).map((detail) => ({
    ...detail,
    loc: detail.loc[0] === "body" ? detail.loc.slice(1) : detail.loc,
  }));
}

// 서버 오류를 폼 칸에 붙인다. 붙일 칸이 없는 오류의 메시지는 돌려준다.
export function applyServerErrors<F extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<F>,
  toPath: (loc: (string | number)[]) => string | null,
): string[] {
  const leftover: string[] = [];
  for (const item of serverFieldErrors(error)) {
    const path = toPath(item.loc);
    if (path) setError(path as FieldPath<F>, { type: "server", message: item.msg });
    else leftover.push(item.msg);
  }
  return leftover;
}
