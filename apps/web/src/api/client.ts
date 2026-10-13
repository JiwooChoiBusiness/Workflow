import createClient from "openapi-fetch";
import { apiFetch, networkError, toApiError } from "./http";
import type { paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "", fetch: (request) => apiFetch(request) });

interface FetchResult<D> {
  data?: D;
  error?: unknown;
  response: Response;
}

// openapi-fetch의 {data, error}를 TanStack Query가 쓰기 좋게 값 또는 ApiError 예외로 바꾼다.
export async function unwrap<D>(pending: Promise<FetchResult<D>>): Promise<D> {
  let result: FetchResult<D>;
  try {
    result = await pending;
  } catch (cause) {
    throw networkError(cause);
  }
  if (!result.response.ok) throw toApiError(result.response, result.error);
  return result.data as D;
}
