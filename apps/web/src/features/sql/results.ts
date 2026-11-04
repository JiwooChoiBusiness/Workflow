/*
 * SQL 결과 표를 그리고 내려받는 데 쓰는 것. 행은 서버 모양 그대로 열 이름 → 값의 객체다(SqlQueryResult.rows,
 * sql_query 노드의 rows 출력). query_sql 도구 결과는 글이라 그 안의 마크다운 표를 읽어 같은 모양으로 바꾼다.
 */

export type ResultRow = Record<string, unknown>;

export interface ResultSet {
  columns: string[];
  rows: ResultRow[];
}

// 표 한 칸에 보일 글. null은 따로 그리므로 여기서는 글자로만 바꾼다.
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// 표 프로그램이 수식으로 읽는 첫 글자(CSV 수식 주입). 서버 runner.py _csv_cell처럼 글 값만 앞에 '를 붙인다.
const FORMULA_START = /^[=+\-@\t\r]/;
// 서버는 DECIMAL, 2^53을 넘는 정수, inf·nan을 JSON에 글로 보낸다(runner.py _value). 원래 값이 수였는지 여기서는
// 알 수 없어 숫자 모양 글은 수로 보고 그대로 둔다. 숫자 모양 글은 수식이 될 수 없다.
const NUMBER_TEXT = /^[+-]?((\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?|inf|nan)$/;

function quoted(text: string): string {
  return /[",\r\n]/.test(text) || text !== text.trim() ? `"${text.replace(/"/g, '""')}"` : text;
}

function neutralized(text: string): string {
  return FORMULA_START.test(text) ? `'${text}` : text;
}

// 열 이름은 모델이 쓴 SQL 별칭(늘 글)이라 숫자 모양이어도 중화한다. 서버 머리글과 같다.
function csvHeader(name: string): string {
  return quoted(neutralized(name));
}

function csvField(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "string") return quoted(cellText(value));
  return quoted(NUMBER_TEXT.test(value) ? value : neutralized(value));
}

// RFC 4180 CSV. 엑셀이 UTF-8로 열도록 BOM을 붙이고, null은 빈 칸이다.
export function toCsv(columns: readonly string[], rows: readonly ResultRow[]): string {
  const lines = [columns.map(csvHeader).join(",")];
  for (const row of rows) lines.push(columns.map((column) => csvField(row[column])).join(","));
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

export function csvFilename(name: string, now: Date = new Date()): string {
  const safe = name.replace(/[\\/:*?"<>|\s]+/g, "_").replace(/^_+|_+$/g, "") || "result";
  const stamp = now.toISOString().slice(0, 19).replace(/[-:T]/g, "");
  return `${safe}-${stamp}.csv`;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

// sql_query 노드 출력(columns·rows)에서 표를 꺼낸다. 4KB 미리보기에서 잘린 값은 건너뛴다.
export function resultSetOf(outputs: unknown): ResultSet | null {
  const out = record(outputs);
  if (!out || !Array.isArray(out.columns)) return null;
  const columns = out.columns.filter((item): item is string => typeof item === "string");
  const rows = Array.isArray(out.rows)
    ? out.rows.flatMap((item) => {
        const row = record(item);
        return row ? [row] : [];
      })
    : [];
  return { columns, rows };
}

// 마크다운 표의 한 줄. 서버 results.cell이 "|"를 "\|"로 바꿔 넣는다.
function splitRow(line: string): string[] {
  const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let current = "";
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (char === "\\" && body[index + 1] === "|") {
      current += "|";
      index += 1;
    } else if (char === "|") {
      cells.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

const SEPARATOR = /^\|(\s*---\s*\|)+$/;

export interface ToolSqlResult {
  // 결과 글에 실은 앞부분 표(llm_rows행까지)
  table: ResultSet | null;
  // 표 아래 요약 줄("3행." 등)
  summary: string;
  // 요약 줄에서 읽은 받은 행 수(잘렸으면 행 상한)와 표에 실은 행 수. 읽지 못하면 null이다.
  rowCount: number | null;
  shown: number | null;
  truncated: boolean;
  // "실행한 SQL:" 뒤부터 CSV 안내 줄 앞까지. 여러 줄일 수 있다.
  executed: string;
  // 잘려서 작업공간에 남긴 CSV(sql/<호출>.csv)
  csvPath: string | null;
}

const EXECUTED = "실행한 SQL:";
const CSV_LINE = /\r?\n전체 결과는 작업공간 (\S+\.csv)에 CSV로 저장했다\.?\s*$/;

// 서버 results.summary_line의 네 모양을 읽는다.
function summaryCounts(summary: string): Pick<ToolSqlResult, "rowCount" | "shown" | "truncated"> {
  if (summary.startsWith("결과 행이 없다")) return { rowCount: 0, shown: 0, truncated: false };
  const cut = /^결과가 (\d+)행을 넘어 잘렸다\. 처음 (\d+)행/.exec(summary);
  if (cut) return { rowCount: Number(cut[1]), shown: Number(cut[2]), truncated: true };
  const part = /^(\d+)행 중 처음 (\d+)행/.exec(summary);
  if (part) return { rowCount: Number(part[1]), shown: Number(part[2]), truncated: false };
  const all = /^(\d+)행\./.exec(summary);
  if (all) return { rowCount: Number(all[1]), shown: Number(all[1]), truncated: false };
  return { rowCount: null, shown: null, truncated: false };
}

/*
 * query_sql 도구의 결과 글을 읽는다. 모양은 <document name="질의 결과" …>표</document>, 요약 줄, "실행한 SQL: …",
 * 잘렸으면 "전체 결과는 작업공간 <경로>에 CSV로 저장했다."이다(apps/api/app/agent/tools/query_sql.py). 메시지
 * 기록에서 다시 그린 카드에는 tool_call.completed.sql이 없어 행 수·잘림·실행한 SQL도 이 글에서 읽는다.
 */
export function parseToolResult(output: string): ToolSqlResult {
  const start = output.indexOf("<document");
  const end = output.indexOf("</document>");
  let table: ResultSet | null = null;
  if (start >= 0 && end > start) {
    const inner = output.slice(output.indexOf(">", start) + 1, end);
    const lines = inner
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.startsWith("|"));
    if (lines.length >= 2 && SEPARATOR.test(lines[1]!.replace(/\s+/g, ""))) {
      const columns = splitRow(lines[0]!);
      const rows = lines.slice(2).map((line) => {
        const cells = splitRow(line);
        return Object.fromEntries(
          columns.map((column, index) => [column, cells[index] === "NULL" ? null : cells[index]]),
        );
      });
      table = { columns, rows };
    }
  }
  const after = end >= 0 ? end + "</document>".length : 0;
  const at = output.indexOf(EXECUTED, after);
  const head = output.slice(after, at >= 0 ? at : undefined);
  const summary =
    head
      .split(/\r?\n/)
      .find((line) => line.trim())
      ?.trim() ?? "";
  let executed = "";
  let csvPath: string | null = null;
  if (at >= 0) {
    // CSV 안내는 늘 마지막 줄이다. SQL 글 안에 같은 문장이 있어도 끝에 붙은 것만 안내로 본다.
    const tail = output.slice(at + EXECUTED.length);
    const csv = CSV_LINE.exec(tail);
    csvPath = csv ? csv[1]! : null;
    executed = (csv ? tail.slice(0, csv.index) : tail).trim();
  }
  return { table, summary, ...summaryCounts(summary), executed, csvPath };
}
