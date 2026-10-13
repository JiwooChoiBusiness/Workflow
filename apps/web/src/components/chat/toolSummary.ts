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
    default:
      return firstLine(JSON.stringify(input));
  }
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
