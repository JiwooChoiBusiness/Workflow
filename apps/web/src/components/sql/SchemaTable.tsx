import { EyeOff } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { SqlTable } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/toggle";
import {
  columnNote,
  setColumn,
  setTable,
  tableNote,
  type TableNotes,
} from "@/features/sql/annotations";
import { cellText } from "@/features/sql/results";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";

// 열 샘플값(준비 때 열마다 3개까지). 숨긴 열은 서버가 샘플을 지운다.
function samplesText(samples: readonly unknown[] | undefined): string {
  return (samples ?? []).map((value) => cellText(value)).join(", ");
}

/*
 * 스키마 표(설계 10.2, 12.3): 표·열·타입·샘플과 설명 칸, 표·열 숨김 스위치. 값은 부르는 쪽(AnnotationEditor)이
 * 들고 있다가 한 번에 저장한다. 숨긴 표의 열은 흐리게 두지만 열 숨김은 그대로 고칠 수 있다.
 */
export function SchemaTable({
  tables,
  notes,
  onChange,
  disabled = false,
}: {
  tables: readonly SqlTable[];
  notes: TableNotes;
  onChange: (next: TableNotes) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation("sql");
  return (
    <div className="flex flex-col gap-3">
      {tables.map((table) => {
        const note = tableNote(notes, table.name);
        return (
          <section
            key={table.name}
            aria-label={`${t("schema.table")} ${table.name}`}
            className={cn(
              "flex flex-col gap-2 rounded-md border border-line bg-surface p-3",
              note.hidden && "bg-sunken",
            )}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm font-medium text-ink">{table.name}</span>
              {table.kind === "view" ? <Badge>{t("schema.view")}</Badge> : null}
              {typeof table.row_count === "number" ? (
                <span className="text-xs text-ink-subtle tabular-nums">
                  {t("schema.rows", { rows: formatNumber(table.row_count) })}
                </span>
              ) : null}
              {note.hidden ? (
                <Badge tone="warn">
                  <EyeOff className="size-3" aria-hidden />
                  {t("schema.tableHidden")}
                </Badge>
              ) : null}
              <label className="ml-auto flex items-center gap-2 text-xs text-ink-muted">
                {t("schema.hidden")}
                <Switch
                  checked={note.hidden}
                  disabled={disabled}
                  aria-label={t("schema.hideTable", { table: table.name })}
                  onCheckedChange={(hidden) => onChange(setTable(notes, table.name, { hidden }))}
                />
              </label>
            </div>
            {table.error ? <p className="text-xs text-warn">{table.error}</p> : null}
            <Input
              value={note.description}
              disabled={disabled}
              placeholder={t("schema.description")}
              aria-label={t("schema.tableDescription", { table: table.name })}
              maxLength={500}
              onChange={(event) =>
                onChange(setTable(notes, table.name, { description: event.target.value }))
              }
            />
            <div className={cn("overflow-x-auto", note.hidden && "opacity-60")}>
              <table className="w-full text-xs">
                <thead className="text-left text-ink-muted">
                  <tr>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      {t("schema.column")}
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      {t("schema.type")}
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      {t("schema.samples")}
                    </th>
                    <th scope="col" className="py-1 pr-3 font-medium">
                      {t("schema.description")}
                    </th>
                    <th scope="col" className="py-1 text-right font-medium">
                      {t("schema.hidden")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {table.columns.map((column) => {
                    const item = columnNote(notes, table.name, column.name);
                    const samples = samplesText(column.samples);
                    return (
                      <tr key={column.name} className={cn(item.hidden && "text-ink-subtle")}>
                        <td className="py-1 pr-3 font-mono whitespace-nowrap text-ink">
                          {column.name}
                        </td>
                        <td className="py-1 pr-3 font-mono whitespace-nowrap text-ink-muted">
                          {column.type}
                        </td>
                        <td className="max-w-56 truncate py-1 pr-3 text-ink-muted" title={samples}>
                          {samples || (
                            <span className="text-ink-subtle">{t("schema.noSamples")}</span>
                          )}
                        </td>
                        <td className="min-w-40 py-1 pr-3">
                          <Input
                            className="h-7"
                            value={item.description}
                            disabled={disabled}
                            maxLength={500}
                            aria-label={t("schema.columnDescription", {
                              table: table.name,
                              column: column.name,
                            })}
                            onChange={(event) =>
                              onChange(
                                setColumn(notes, table.name, column.name, {
                                  description: event.target.value,
                                }),
                              )
                            }
                          />
                        </td>
                        <td className="py-1 text-right">
                          <Switch
                            checked={item.hidden}
                            disabled={disabled}
                            aria-label={t("schema.hideColumn", {
                              table: table.name,
                              column: column.name,
                            })}
                            onCheckedChange={(hidden) =>
                              onChange(setColumn(notes, table.name, column.name, { hidden }))
                            }
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        );
      })}
    </div>
  );
}
