// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  knowledgeKeys,
  previewBody,
  uploadBody,
  useRefreshWhenSettled,
  useSegmentsFollowDocument,
} from "./api";

function withClient() {
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return { invalidate, wrapper };
}

describe("지식 API 요청 모양", () => {
  it("파일 올리기는 files 여러 개와 JSON 옵션 칸 data다. 기본값 옵션은 보내지 않는다", () => {
    const a = new File(["가"], "a.txt", { type: "text/plain" });
    const b = new File(["나"], "b.md", { type: "text/markdown" });
    const plain = uploadBody({ kind: "files", files: [a, b], options: { allow_duplicate: false } });
    expect(plain.json).toBeUndefined();
    expect(plain.body?.getAll("files")).toHaveLength(2);
    expect(plain.body?.get("data")).toBeNull();

    const withOptions = uploadBody({
      kind: "files",
      files: [a],
      options: {
        allow_duplicate: true,
        extract_mode: "model",
        process_rule: { child: { max_tokens: 128 } },
      },
    });
    expect(JSON.parse(String(withOptions.body?.get("data")))).toEqual({
      allow_duplicate: true,
      extract_mode: "model",
      process_rule: { child: { max_tokens: 128 } },
    });
  });

  it("글은 JSON {name, text}이고 PDF 읽기 방식은 붙이지 않는다", () => {
    expect(
      uploadBody({
        kind: "text",
        name: "메모",
        text: "본문",
        options: { extract_mode: "model", allow_duplicate: false },
      }),
    ).toEqual({ json: { name: "메모", text: "본문" } });
  });

  it("미리보기는 파일 하나(file)나 글이다", () => {
    const file = new File(["x"], "a.pdf", { type: "application/pdf" });
    const form = previewBody({
      kind: "file",
      file,
      processRule: { general: { max_tokens: 300, overlap_tokens: 0 } },
    });
    expect(form.body?.get("file")).toBeInstanceOf(File);
    expect(JSON.parse(String(form.body?.get("data")))).toEqual({
      process_rule: { general: { max_tokens: 300, overlap_tokens: 0 } },
    });
    expect(previewBody({ kind: "text", text: "본문" })).toEqual({ json: { text: "본문" } });
  });

  it("문서·청크 키는 KB 키 아래에 있어 KB를 무효화하면 같이 다시 읽는다", () => {
    const detail = knowledgeKeys.detail("kb1");
    expect(knowledgeKeys.documents("kb1", { page: 1, limit: 20 }).slice(0, 2)).toEqual(detail);
    expect(knowledgeKeys.segmentsAll("kb1", "d1").slice(0, 4)).toEqual(
      knowledgeKeys.document("kb1", "d1"),
    );
    expect(knowledgeKeys.stats("kb1").slice(0, 2)).toEqual(detail);
  });
});

describe("다시 읽는 때", () => {
  it("진행 중 문서 수가 0이 되는 순간에만 통계와 문서 표를 한 번 다시 읽는다", () => {
    const { invalidate, wrapper } = withClient();
    const { rerender } = renderHook(({ count }) => useRefreshWhenSettled("kb1", count), {
      wrapper,
      initialProps: { count: 0 },
    });
    expect(invalidate).not.toHaveBeenCalled();
    rerender({ count: 2 });
    rerender({ count: 1 });
    expect(invalidate).not.toHaveBeenCalled();

    rerender({ count: 0 });
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      knowledgeKeys.stats("kb1"),
      knowledgeKeys.documentsAll("kb1"),
    ]);
    rerender({ count: 0 });
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("청크 목록은 문서의 상태나 진행이 바뀔 때만 다시 읽는다", () => {
    type Followed = Parameters<typeof useSegmentsFollowDocument>[1];
    const { invalidate, wrapper } = withClient();
    const indexing: Followed = {
      id: "d1",
      indexing_status: "indexing",
      completed_segments: 3,
      total_segments: 10,
    };
    const { rerender } = renderHook(
      ({ document }: { document: Followed }) => useSegmentsFollowDocument("kb1", document),
      { wrapper, initialProps: { document: { ...indexing } } },
    );
    rerender({ document: { ...indexing } });
    expect(invalidate).not.toHaveBeenCalled();

    rerender({ document: { ...indexing, completed_segments: 6 } });
    rerender({ document: { ...indexing, indexing_status: "completed", completed_segments: 10 } });
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      knowledgeKeys.segmentsAll("kb1", "d1"),
      knowledgeKeys.segmentsAll("kb1", "d1"),
    ]);
  });
});
