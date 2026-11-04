// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { paths } from "@/api/schema";
import "@/features/knowledge/i18n";
import { useDatasetExamples } from "./KbApiPanel";

type Method = "get" | "post";
type MethodsOf<P extends keyof paths> = {
  [M in Method]: [NonNullable<paths[P][M]>] extends [never] ? never : M;
}[Method];
type Route = { [P in keyof paths]: { path: P; method: MethodsOf<P> } }[keyof paths];

// 서버 OpenAPI(schema.d.ts)에 없는 경로나 메서드를 적으면 타입 검사에서 걸린다.
const ROUTES: Route[] = [
  { path: "/api/v1/datasets/{dataset_id}/retrieve", method: "post" },
  { path: "/api/v1/datasets/{dataset_id}/document/create-by-text", method: "post" },
  { path: "/api/v1/datasets/{dataset_id}/documents/{batch}/indexing-status", method: "get" },
  { path: "/api/v1/datasets/{dataset_id}/documents", method: "get" },
];

describe("KB API 호출 예시", () => {
  it("서비스 API datasets 경로와 메서드를 그대로 쓴다", () => {
    const { result } = renderHook(() => useDatasetExamples("kb-1"));

    const used = result.current.map((example) => {
      const url = /curl "?([^"\s]+)/.exec(example.command)?.[1] ?? "";
      const path = url
        .replace(window.location.origin, "")
        .replace(/\?.*$/, "")
        .replace("kb-1", "{dataset_id}")
        .replace("<batch>", "{batch}");
      return { path, method: example.command.includes("--data-binary") ? "post" : "get" };
    });

    expect(used).toEqual(ROUTES);
    expect(result.current.every((example) => example.command.includes("Bearer $API_KEY"))).toBe(
      true,
    );
  });
});
