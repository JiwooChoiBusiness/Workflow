import { describe, expect, it } from "vitest";
import schema from "../../../../packages/event-schema/run-events.schema.json";
import { contract } from "@/test/contract";
import { fixtures } from "@/test/events";
import { isKnownEventType, isTerminalEventType, type RunEvent } from "./run-events";
import { RUN_EVENT_TYPES, TERMINAL_EVENT_TYPES, TRANSIENT_EVENT_TYPES } from "./run-events.gen";

interface Node {
  type?: string;
  enum?: unknown[];
  const?: unknown;
  anyOf?: Node[];
  items?: Node;
  $ref?: string;
  properties?: Record<string, Node>;
  required?: string[];
}

const defs = schema.$defs as Record<string, Node>;
const variants = new Map(
  (schema.oneOf as { title: string; properties: { data: Node } }[]).map((variant) => [
    variant.title,
    variant.properties.data,
  ]),
);

// 스키마에 쓰인 구성(type·required·enum·const·anyOf·items·$ref)만 확인하는 작은 검사기.
function problems(value: unknown, node: Node, path: string): string[] {
  if (node.$ref) return problems(value, defs[node.$ref.replace("#/$defs/", "")], path);
  if (node.anyOf) {
    const fits = node.anyOf.some((option) => problems(value, option, path).length === 0);
    return fits ? [] : [`${path}: anyOf에 맞는 것이 없다 (${JSON.stringify(value)})`];
  }
  if (node.const !== undefined && value !== node.const) return [`${path}: ${String(value)}`];
  if (node.enum && !node.enum.includes(value)) return [`${path}: ${String(value)}는 enum 밖`];
  switch (node.type) {
    case "null":
      return value === null ? [] : [`${path}: null이 아니다`];
    case "string":
      return typeof value === "string" ? [] : [`${path}: 문자열이 아니다`];
    case "integer":
      return Number.isInteger(value) ? [] : [`${path}: 정수가 아니다`];
    case "number":
      return typeof value === "number" ? [] : [`${path}: 숫자가 아니다`];
    case "boolean":
      return typeof value === "boolean" ? [] : [`${path}: 참거짓이 아니다`];
    case "array":
      if (!Array.isArray(value)) return [`${path}: 배열이 아니다`];
      return node.items
        ? value.flatMap((item, index) => problems(item, node.items!, `${path}[${index}]`))
        : [];
    case "object": {
      if (typeof value !== "object" || value === null) return [`${path}: 객체가 아니다`];
      const record = value as Record<string, unknown>;
      const missing = (node.required ?? [])
        .filter((key) => !(key in record))
        .map((key) => `${path}.${key}: 없다`);
      const nested = Object.entries(node.properties ?? {}).flatMap(([key, child]) =>
        key in record ? problems(record[key], child, `${path}.${key}`) : [],
      );
      return [...missing, ...nested];
    }
    default:
      return [];
  }
}

function check(events: readonly RunEvent[]): string[] {
  return events.flatMap((event, index) => {
    const data = variants.get(event.type);
    if (!data) return [`[${index}] 모르는 이벤트 ${event.type}`];
    if (event.type === "ping") return [];
    return problems(event.data, data, `[${index}] ${event.type}`);
  });
}

describe("런 이벤트 스키마(packages/event-schema)와 화면 타입", () => {
  it("생성한 타입의 이벤트 목록·종료 이벤트·저장하지 않는 이벤트가 스키마와 같다", () => {
    expect([...RUN_EVENT_TYPES]).toEqual([...variants.keys()]);
    expect([...TERMINAL_EVENT_TYPES]).toEqual(schema["x-terminal-types"]);
    expect(isTerminalEventType("run.completed")).toBe(true);
    expect(isTerminalEventType("workflow.completed")).toBe(true);
    expect(isTerminalEventType("run.error")).toBe(false);
    expect([...TRANSIENT_EVENT_TYPES]).toEqual(schema["x-transient-types"]);
    for (const type of RUN_EVENT_TYPES) expect(isKnownEventType(type)).toBe(true);
    expect(isKnownEventType("something.new")).toBe(false);
  });

  it("실제 서버에서 받은 이벤트가 스키마에 맞는다(검사기 확인)", () => {
    const { approval, autoApprove, stopWaiting, question } = contract;
    const events = [
      ...approval.send.events,
      ...approval.resume,
      ...autoApprove.send,
      ...autoApprove.resume,
      ...autoApprove.autoRun,
      ...stopWaiting.send,
      ...stopWaiting.after,
      ...question.send,
      ...question.answer,
    ];
    expect(events.length).toBeGreaterThan(80);
    expect(check(events)).toEqual([]);
  });

  it("손으로 만든 화면 개발용 픽스처도 스키마에 맞는다", () => {
    for (const [name, events] of Object.entries(fixtures)) {
      expect({ name, problems: check(events) }).toEqual({ name, problems: [] });
    }
  });
});
