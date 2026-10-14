// packages/event-schema/run-events.schema.json(단일 출처, 설계 13.2)에서 src/api/run-events.gen.ts를 만든다.
//   node scripts/gen-run-events.ts          다시 만든다
//   node scripts/gen-run-events.ts --check  스키마와 같은지만 확인한다(pnpm test가 먼저 돌린다)
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { format, resolveConfig } from "prettier";

interface SchemaNode {
  type?: string;
  format?: string;
  enum?: (string | number | boolean | null)[];
  const?: string | number | boolean | null;
  anyOf?: SchemaNode[];
  items?: SchemaNode;
  $ref?: string;
  properties?: Record<string, SchemaNode>;
  required?: string[];
  additionalProperties?: boolean | SchemaNode;
  description?: string;
  title?: string;
}

interface EventSchema {
  "x-schema-version": number;
  "x-transient-types": string[];
  "x-terminal-types": string[];
  oneOf: { title: string; properties: { data: SchemaNode } }[];
  $defs: Record<string, SchemaNode>;
}

const schemaPath = fileURLToPath(
  new URL("../../../packages/event-schema/run-events.schema.json", import.meta.url),
);
const outPath = fileURLToPath(new URL("../src/api/run-events.gen.ts", import.meta.url));

function refName(ref: string): string {
  const prefix = "#/$defs/";
  if (!ref.startsWith(prefix)) throw new Error(`모르는 $ref: ${ref}`);
  return ref.slice(prefix.length);
}

function literal(value: string | number | boolean | null): string {
  return JSON.stringify(value);
}

function tsType(node: SchemaNode): string {
  if (node.$ref) return refName(node.$ref);
  if (node.anyOf) return [...new Set(node.anyOf.map(tsType))].join(" | ");
  if (node.const !== undefined) return literal(node.const);
  if (node.enum) return node.enum.map(literal).join(" | ");
  switch (node.type) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array":
      return node.items ? `${wrap(tsType(node.items))}[]` : "unknown[]";
    case "object":
      return node.properties ? objectType(node) : "Record<string, unknown>";
    case undefined:
      // 타입이 없는 필드(Pydantic Any)
      return "unknown";
    default:
      throw new Error(`모르는 타입: ${node.type}`);
  }
}

function wrap(type: string): string {
  return type.includes("|") ? `(${type})` : type;
}

function objectType(node: SchemaNode): string {
  const required = new Set(node.required ?? []);
  const fields = Object.entries(node.properties ?? {}).map(
    // 기본값이 있는 필드는 스키마의 required에 없어 선택으로 둔다. 서버는 늘 값을 채워 보낸다.
    ([name, prop]) => `${name}${required.has(name) ? "" : "?"}: ${tsType(prop)};`,
  );
  return `{ ${fields.join(" ")} }`;
}

function declaration(name: string, node: SchemaNode): string {
  const doc = node.description ? `/** ${node.description} */\n` : "";
  if (!node.properties || Object.keys(node.properties).length === 0) {
    return `${doc}export type ${name} = Record<string, never>;`;
  }
  return `${doc}export interface ${name} ${objectType(node)}`;
}

function generate(schema: EventSchema): string {
  const defs = Object.keys(schema.$defs)
    .sort()
    .map((name) => declaration(name, schema.$defs[name]));
  const map = schema.oneOf.map(
    (variant) => `${literal(variant.title)}: ${tsType(variant.properties.data)};`,
  );
  const types = schema.oneOf.map((variant) => variant.title);
  return [
    "// packages/event-schema/run-events.schema.json에서 scripts/gen-run-events.ts가 만든 파일이다. 손으로 고치지 않는다.",
    "",
    `export const RUN_EVENT_SCHEMA_VERSION = ${schema["x-schema-version"]};`,
    `export const TERMINAL_EVENT_TYPES = ${JSON.stringify(schema["x-terminal-types"])} as const;`,
    `export const TRANSIENT_EVENT_TYPES = ${JSON.stringify(schema["x-transient-types"])} as const;`,
    `export const RUN_EVENT_TYPES = ${JSON.stringify(types)} as const;`,
    "",
    defs.join("\n\n"),
    "",
    `export interface RunEventDataMap { ${map.join(" ")} }`,
    "",
  ].join("\n");
}

async function main(): Promise<number> {
  const schema = JSON.parse(readFileSync(schemaPath, "utf8")) as EventSchema;
  const config = (await resolveConfig(outPath)) ?? {};
  const text = await format(generate(schema), { ...config, filepath: outPath });
  if (process.argv.includes("--check")) {
    let current = "";
    try {
      current = readFileSync(outPath, "utf8");
    } catch {
      // 파일이 없으면 다르다고 본다.
    }
    if (current.replace(/\r\n/g, "\n") !== text) {
      console.error(
        "src/api/run-events.gen.ts가 이벤트 스키마와 다르다. node scripts/gen-run-events.ts로 다시 만든다.",
      );
      return 1;
    }
    return 0;
  }
  writeFileSync(outPath, text, "utf8");
  return 0;
}

process.exitCode = await main();
