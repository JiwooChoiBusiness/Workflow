import { cellText, type ResultRow } from "@/features/sql/results";
import { cn } from "@/lib/cn";

const CELL_CHARS = 200;

function Cell({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="text-ink-subtle italic">NULL</span>;
  }
  const text = cellText(value);
  const short = text.length > CELL_CHARS ? `${text.slice(0, CELL_CHARS)}…` : text;
  return (
    <span
      className={typeof value === "number" ? "tabular-nums" : undefined}
      title={short === text ? undefined : text}
    >
      {short}
    </span>
  );
}

/*
 * SQL 결과 표(설계 10.6). SQL 시험·노드 런 카드·도구 카드가 같이 쓴다. 앞 limit행만 그리고, 나머지 행 수는
 * 부르는 쪽이 요약 줄로 알린다. 문구가 없어 어느 화면 묶음에 들어가도 번역 파일을 끌고 오지 않는다.
 */
export function ResultTable({
  columns,
  rows,
  label,
  limit = 50,
  className,
}: {
  columns: readonly string[];
  rows: readonly ResultRow[];
  label: string;
  limit?: number;
  className?: string;
}) {
  const shown = rows.slice(0, limit);
  return (
    <div className={cn("max-h-80 overflow-auto rounded-md border border-line", className)}>
      <table aria-label={label} className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-sunken text-left text-ink-muted">
          <tr>
            {columns.map((column) => (
              <th
                key={column}
                scope="col"
                className="border-b border-line px-2 py-1.5 font-mono font-medium whitespace-nowrap"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {shown.map((row, index) => (
            <tr key={index} className="align-top">
              {columns.map((column) => (
                <td key={column} className="max-w-72 px-2 py-1 break-words text-ink">
                  <Cell value={row[column]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
