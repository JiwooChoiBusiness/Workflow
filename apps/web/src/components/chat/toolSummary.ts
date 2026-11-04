export const SEARCH_KNOWLEDGE = "search_knowledge";
export const QUERY_SQL = "query_sql";
export const DESCRIBE_SQL_SOURCE = "describe_sql_source";

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function firstLine(value: string, max = 160): string {
  const line = value.split(/\r?\n/, 1)[0] ?? "";
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

// 도구 카드 머리에 한 줄로 보일 입력 요약(설계 7.3 "이름·입력 요약")
export function summarizeInput(
  name: string,
  input: Record<string, unknown> | null,
  inputText: string,
): string {
  if (!input) return firstLine(inputText);
  switch (name) {
    case "shell":
      return firstLine(text(input.command));
    case "run_python":
      return firstLine(text(input.code));
    case "install_packages":
      return Array.isArray(input.packages) ? input.packages.map(String).join(", ") : "";
    case "read_file":
    case "write_file":
    case "edit_file":
      return text(input.path);
    case "list_files":
      return text(input.path) || ".";
    case "http_request":
      return `${text(input.method) || "GET"} ${text(input.url)}`;
    case "ask_user":
      return firstLine(text(input.question));
    case "call_agent":
      return `${text(input.agent)}: ${firstLine(text(input.task))}`;
    case SEARCH_KNOWLEDGE: {
      const base = text(input.knowledge_base);
      return base ? `${firstLine(text(input.query))} · ${base}` : firstLine(text(input.query));
    }
    case QUERY_SQL: {
      const source = text(input.source);
      const sql = firstLine(text(input.sql).trim());
      return source ? `${source} · ${sql}` : sql;
    }
    case DESCRIBE_SQL_SOURCE:
      return text(input.source);
    default:
      return firstLine(JSON.stringify(input));
  }
}

export interface KnowledgeHit {
  position: number;
  documentName: string;
  datasetName: string;
}

const ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#x27;": "'",
};

function unescapeAttribute(value: string): string {
  return value.replace(/&(?:amp|lt|gt|quot|#x27);/g, (entity) => ENTITIES[entity] ?? entity);
}

// search_knowledge 결과 글의 <document index="n" name="…" source="…"> 머리를 순위대로 읽는다. 메시지
// 기록에서 다시 그린 카드에는 인용 목록이 없어 결과 글로 요약한다(서버 search_knowledge.result_text).
// 자료는 빈 줄 하나로 이어 붙으므로 글 처음이나 앞 자료의 </document> 바로 뒤에 온 머리만 센다. 자료 본문에
// 든 <document …>까지 세면 결과 수가 부푼다.
const DOCUMENT_HEAD =
  /(?:^|<\/document>\n\n)<document index="(\d+)"(?: name="([^"]*)")?(?: source="([^"]*)")?/g;

export function knowledgeHits(output: string): KnowledgeHit[] {
  return [...output.matchAll(DOCUMENT_HEAD)].map((match) => ({
    position: Number(match[1]),
    documentName: unescapeAttribute(match[2] ?? ""),
    datasetName: unescapeAttribute(match[3] ?? ""),
  }));
}

const SENSITIVE_HEADERS = new Set(["authorization", "cookie", "x-api-key", "proxy-authorization"]);

// 서버가 이벤트를 기록할 때 이미 가리지만(설계 3.9, S8) 화면에서도 한 번 더 가린다.
export function maskedHeaders(headers: unknown): [string, string][] {
  if (typeof headers !== "object" || headers === null) return [];
  return Object.entries(headers as Record<string, unknown>).map(([key, value]) => [
    key,
    SENSITIVE_HEADERS.has(key.toLowerCase()) ? "***" : String(value),
  ]);
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
