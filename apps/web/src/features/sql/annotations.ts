/*
 * 스키마 표의 설명·숨김 편집(설계 10.2 주석). 서버 PATCH .../annotations는 표·열 주석 전체를 바꾸므로, 화면은
 * 저장된 주석 위에 고친 것만 얹은 사본을 보낸다. 키는 스냅샷의 표·열 이름 그대로이고, 숨김 비교는 서버처럼
 * 대소문자를 가리지 않는다(app/knowledge/sql/sources.py hidden_columns).
 */
import type { SqlAnnotationsUpdate, SqlColumnNote, SqlTableNote } from "@/api/types";

export type TableNotes = Record<string, SqlTableNote>;

export interface AnnotationDraft {
  tables: TableNotes;
  glossary: string;
}

const EMPTY_TABLE: SqlTableNote = { description: "", hidden: false, columns: {} };
const EMPTY_COLUMN: SqlColumnNote = { description: "", hidden: false };

export function tableNote(tables: TableNotes, table: string): SqlTableNote {
  return tables[table] ?? EMPTY_TABLE;
}

export function columnNote(tables: TableNotes, table: string, column: string): SqlColumnNote {
  return tableNote(tables, table).columns?.[column] ?? EMPTY_COLUMN;
}

function withTable(tables: TableNotes, table: string, change: Partial<SqlTableNote>): TableNotes {
  const current = tableNote(tables, table);
  return { ...tables, [table]: { ...current, columns: { ...current.columns }, ...change } };
}

export function setTable(
  tables: TableNotes,
  table: string,
  change: Partial<Pick<SqlTableNote, "description" | "hidden">>,
): TableNotes {
  return withTable(tables, table, change);
}

export function setColumn(
  tables: TableNotes,
  table: string,
  column: string,
  change: Partial<SqlColumnNote>,
): TableNotes {
  const current = tableNote(tables, table);
  const columns = {
    ...current.columns,
    [column]: { ...columnNote(tables, table, column), ...change },
  };
  return withTable(tables, table, { columns });
}

// 숨긴 표와 열의 이름 집합("표" 또는 "표.열", 소문자). 숨김이 바뀌었는지 볼 때 쓴다.
export function hiddenKeys(tables: TableNotes): Set<string> {
  const keys = new Set<string>();
  for (const [table, note] of Object.entries(tables)) {
    const name = table.toLowerCase();
    if (note.hidden) keys.add(name);
    for (const [column, item] of Object.entries(note.columns ?? {})) {
      if (item.hidden) keys.add(`${name}.${column.toLowerCase()}`);
    }
  }
  return keys;
}

// 저장하면 질의 파일(또는 SQLite 사본)을 다시 만들어야 하는 변화인지. 설명만 바뀌면 아니다.
export function hiddenChanged(saved: TableNotes, draft: TableNotes): boolean {
  const before = hiddenKeys(saved);
  const after = hiddenKeys(draft);
  return before.size !== after.size || [...after].some((key) => !before.has(key));
}

function hasContent(note: SqlTableNote): boolean {
  return (
    note.hidden ||
    note.description.trim() !== "" ||
    Object.values(note.columns ?? {}).some((item) => item.hidden || item.description.trim() !== "")
  );
}

// 빈 주석(설명 없음, 숨김 아님)은 빼고 보낸다. 서버는 받은 것으로 주석 전체를 바꾼다.
export function annotationsUpdate(draft: AnnotationDraft, prepare = true): SqlAnnotationsUpdate {
  const tables: TableNotes = {};
  for (const [name, note] of Object.entries(draft.tables)) {
    if (!hasContent(note)) continue;
    const columns = Object.fromEntries(
      Object.entries(note.columns ?? {})
        .filter(([, item]) => item.hidden || item.description.trim() !== "")
        .map(([column, item]) => [
          column,
          { description: item.description.trim(), hidden: item.hidden },
        ]),
    );
    tables[name] = { description: note.description.trim(), hidden: note.hidden, columns };
  }
  return { tables, glossary: draft.glossary, prepare };
}

function canonical(draft: AnnotationDraft): string {
  const tables = annotationsUpdate(draft).tables;
  return JSON.stringify(
    Object.keys(tables)
      .sort()
      .map((name) => {
        const note = tables[name]!;
        const columns = note.columns ?? {};
        return [
          name,
          note.description,
          note.hidden,
          Object.keys(columns)
            .sort()
            .map((column) => [column, columns[column]!.description, columns[column]!.hidden]),
        ];
      }),
  );
}

// 고친 것이 있는지(저장 단추). 빈 주석과 키 순서는 따지지 않는다.
export function sameDraft(a: AnnotationDraft, b: AnnotationDraft): boolean {
  return a.glossary === b.glossary && canonical(a) === canonical(b);
}
