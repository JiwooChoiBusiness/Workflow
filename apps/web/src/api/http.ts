const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// 로그인 실패와 로그인 여부 확인의 401은 화면이 직접 처리한다. 나머지 401은 세션이 끝난 것이다.
const SELF_HANDLED_401 = new Set(["/api/console/auth/login", "/api/console/auth/me"]);

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  // 429 응답의 Retry-After(초)
  readonly retryAfter: number | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: unknown,
    retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryAfter = retryAfter;
  }
}

interface ErrorBodyShape {
  error: { code: string; message: string; details?: unknown };
}

function isErrorBody(value: unknown): value is ErrorBodyShape {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

export function toApiError(response: Response, body: unknown): ApiError {
  const retryHeader = Number(response.headers.get("Retry-After"));
  const retryAfter = Number.isFinite(retryHeader) && retryHeader > 0 ? retryHeader : null;
  if (isErrorBody(body)) {
    const { code, message, details } = body.error;
    return new ApiError(response.status, code, message, details, retryAfter);
  }
  return new ApiError(
    response.status,
    `http_${response.status}`,
    response.statusText || `HTTP ${response.status}`,
    undefined,
    retryAfter,
  );
}

export function networkError(cause: unknown): ApiError {
  if (cause instanceof ApiError) return cause;
  const message = cause instanceof Error ? cause.message : String(cause);
  return new ApiError(0, "network_error", message);
}

export function isApiError(value: unknown, status?: number): value is ApiError {
  return value instanceof ApiError && (status === undefined || value.status === status);
}

// 모든 API 요청이 거치는 fetch. openapi-fetch 클라이언트, 멀티파트 업로드, SSE가 같이 쓴다.
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const request = new Request(input, { credentials: "same-origin", ...init });
  if (UNSAFE_METHODS.has(request.method)) {
    // 서버 CSRF 검사는 변경 요청에 JSON Content-Type이나 이 헤더를 요구한다(설계 6.1).
    // 본문 없는 POST와 멀티파트 업로드도 통과하도록 항상 붙인다.
    request.headers.set("X-Requested-With", "fetch");
  }
  const response = await fetch(request);
  if (response.status === 401 && !SELF_HANDLED_401.has(new URL(request.url).pathname)) {
    unauthorizedHandler?.();
  }
  return response;
}

type Query = Record<string, string | number | boolean | null | undefined>;

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

interface RequestOptions {
  json?: unknown;
  body?: BodyInit;
  query?: Query;
  signal?: AbortSignal;
}

// 멀티파트 업로드처럼 openapi-fetch로 보내기 번거로운 요청에 쓴다. 응답 타입은 schema.d.ts에서 가져온다.
export async function requestJson<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers = new Headers();
  let body = options.body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  let response: Response;
  try {
    response = await apiFetch(withQuery(path, options.query), {
      method,
      headers,
      body,
      signal: options.signal,
    });
  } catch (cause) {
    throw networkError(cause);
  }
  const text = response.status === 204 ? "" : await response.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!response.ok) throw toApiError(response, parsed);
  return parsed as T;
}

// 업로드는 multipart의 file 필드 하나다(설계 6.2). Content-Type은 브라우저가 경계값과 함께 붙인다.
export function uploadFile<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return requestJson<T>("POST", path, { body: form });
}
