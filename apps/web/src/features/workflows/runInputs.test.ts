import { describe, expect, it } from "vitest";
import type { FlowField } from "@/api/types";
import type { FlowGraph, FlowNode } from "./graph";
import { initialValues, serverInputErrors, startFieldsOf, toRunInputs } from "./runInputs";

function field(name: string, type: FlowField["type"], extra: Partial<FlowField> = {}): FlowField {
  return { name, type, label: "", required: false, default: null, options: null, ...extra };
}

describe("런 입력 폼", () => {
  it("초안 start 필드를 웹앱 필드 모양으로 읽고 모르는 타입은 글로 본다", () => {
    const start: FlowNode = {
      id: "start",
      type: "start",
      version: 1,
      title: "",
      config: {
        fields: [
          { name: "q", type: "paragraph", label: "질문", required: true },
          { name: "n", type: "number", default: 3 },
          { name: "", type: "string" },
          { name: "x", type: "weird", options: ["a", 1] },
        ],
      },
      error: { strategy: "fail", default_outputs: {} },
      retry: { max_attempts: 0, interval_ms: 1000 },
      parent_id: null,
      position: { x: 0, y: 0 },
      ui: {},
    };
    const graph: FlowGraph = { nodes: [start], edges: [], viewport: {} };
    expect(startFieldsOf(graph)).toEqual([
      field("q", "paragraph", { label: "질문", required: true, max_length: null }),
      field("n", "number", { default: 3, max_length: null }),
      field("x", "string", { options: ["a", "1"], max_length: null }),
    ]);
  });

  it("빈 선택 칸은 보내지 않고 필수 칸·숫자 모양을 먼저 본다", () => {
    const fields = [
      field("q", "string", { required: true }),
      field("n", "number"),
      field("flag", "boolean", { required: true }),
      field("pick", "select", { options: ["a", "b"] }),
      field("doc", "file"),
      field("docs", "file_list"),
    ];
    const values = initialValues(fields);
    expect(values).toEqual({ q: "", n: "", flag: null, pick: "", doc: [], docs: [] });
    expect(toRunInputs(fields, values)).toEqual({
      inputs: {},
      problems: { q: "required", flag: "required" },
    });
    expect(toRunInputs(fields, { ...values, q: "안녕", n: "1e3x", flag: false }).problems).toEqual({
      n: "number",
    });
    const files = [
      { id: "f1", filename: "a.txt", size: 1 },
      { id: "f2", filename: "b.txt", size: 2 },
    ];
    expect(
      toRunInputs(fields, {
        q: "안녕",
        n: " 2.5 ",
        flag: false,
        pick: "b",
        doc: files,
        docs: files,
      }),
    ).toEqual({
      inputs: { q: "안녕", n: 2.5, flag: false, pick: "b", doc: "f1", docs: ["f1", "f2"] },
      problems: {},
    });
  });

  it("기본값으로 폼을 채운다", () => {
    expect(
      initialValues([
        field("q", "string", { default: "기본" }),
        field("n", "number", { default: 7 }),
        field("flag", "boolean", { default: true }),
      ]),
    ).toEqual({ q: "기본", n: "7", flag: true });
  });

  it("서버 invalid_inputs의 details를 칸 오류로 바꾼다", () => {
    expect(serverInputErrors({ q: "필수 입력이다", n: 3 })).toEqual({ q: "필수 입력이다" });
    expect(serverInputErrors(null)).toEqual({});
  });
});
