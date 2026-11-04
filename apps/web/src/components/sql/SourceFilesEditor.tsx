import { FileSpreadsheet, Trash2, Upload } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import type { SqlEncoding, SqlFilePatch, SqlSourceFileOut, SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { useDeleteSqlFile, usePatchSqlSource, useUploadSqlFiles } from "@/features/sql/api";
import { useSqlError } from "@/features/sql/useSqlError";
import { TABLE_NAME } from "@/features/sql/validation";
import { cn } from "@/lib/cn";
import { formatBytes } from "@/lib/format";

const MAX_FILES = 20;
// 서버 FORMATS(app/knowledge/sql/sources.py)
const ACCEPT = { files: [".csv", ".tsv", ".tab", ".xlsx"], sqlite: [".db", ".sqlite", ".sqlite3"] };
const ENCODINGS: readonly SqlEncoding[] = ["auto", "utf-8", "cp949", "euc-kr", "utf-16", "latin-1"];

interface RowDraft {
  table: string;
  sheet: string;
  header: boolean;
  encoding: SqlEncoding;
  all_varchar: boolean;
}

function rowOf(file: SqlSourceFileOut): RowDraft {
  return {
    table: file.table ?? "",
    sheet: file.sheet ?? "",
    header: file.options.header,
    encoding: file.options.encoding,
    all_varchar: file.options.all_varchar,
  };
}

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLowerCase() : "";
}

// 바뀐 칸만 담은 PATCH 항목. 옵션은 셋 중 하나라도 바뀌면 통째로 보낸다.
function filePatch(file: SqlSourceFileOut, row: RowDraft): SqlFilePatch | null {
  const base = rowOf(file);
  const patch: SqlFilePatch = { file_id: file.file_id };
  let changed = false;
  if (file.format !== "sqlite" && row.table.trim() !== base.table) {
    patch.table = row.table.trim();
    changed = true;
  }
  if (file.format === "xlsx" && row.sheet.trim() !== base.sheet) {
    patch.sheet = row.sheet.trim() || null;
    changed = true;
  }
  if (
    row.header !== base.header ||
    row.encoding !== base.encoding ||
    row.all_varchar !== base.all_varchar
  ) {
    patch.options = { header: row.header, encoding: row.encoding, all_varchar: row.all_varchar };
    changed = true;
  }
  return changed ? patch : null;
}

/*
 * 데이터 소스 파일(설계 10.2): 올리기·지우기와 파일마다 표 이름·옵션(머리글, 인코딩 자동·cp949, 모든 열을 글로,
 * xlsx 시트). 표 이름·옵션을 바꿔 저장하면 상태가 stale이 되어 다시 준비해야 한다. SQLite 소스는 파일 하나이고
 * 다시 올리면 바꾼다.
 */
export function SourceFilesEditor({ source }: { source: SqlSourceOut }) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const upload = useUploadSqlFiles(source.id);
  const patch = usePatchSqlSource(source.id);
  const removeFile = useDeleteSqlFile(source.id);
  const errorText = useSqlError();
  const inputRef = useRef<HTMLInputElement>(null);
  const [edits, setEdits] = useState<Record<string, RowDraft>>({});
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [removing, setRemoving] = useState<SqlSourceFileOut | null>(null);
  const sqlite = source.kind === "sqlite";
  const accept = sqlite ? ACCEPT.sqlite : ACCEPT.files;

  const rows = source.files.map((file) => ({ file, row: edits[file.file_id] ?? rowOf(file) }));
  const patches = rows.flatMap(({ file, row }) => {
    const found = filePatch(file, row);
    return found ? [found] : [];
  });
  const tableCounts = new Map<string, number>();
  for (const { row, file } of rows) {
    if (file.format === "sqlite") continue;
    const name = row.table.trim();
    tableCounts.set(name, (tableCounts.get(name) ?? 0) + 1);
  }
  function tableProblem(row: RowDraft): string | undefined {
    const name = row.table.trim();
    if (!TABLE_NAME.test(name)) return t("files.tableInvalid");
    if ((tableCounts.get(name) ?? 0) > 1) return t("files.tableTaken");
    return undefined;
  }
  const invalid = rows.some(({ file, row }) => file.format !== "sqlite" && tableProblem(row));

  function edit(file: SqlSourceFileOut, change: Partial<RowDraft>) {
    setEdits((current) => ({
      ...current,
      [file.file_id]: { ...(current[file.file_id] ?? rowOf(file)), ...change },
    }));
  }

  function send(files: File[]) {
    setProblem(null);
    if (files.length === 0) return;
    const wrong = files.find((file) => !accept.includes(extensionOf(file.name)));
    if (wrong) {
      setProblem(t("files.unsupported", { name: wrong.name }));
      return;
    }
    if (sqlite && files.length > 1) {
      setProblem(t("files.oneOnly"));
      return;
    }
    if (!sqlite && source.files.length + files.length > MAX_FILES) {
      setProblem(t("files.tooMany", { max: MAX_FILES }));
      return;
    }
    upload.mutate(files, {
      onSuccess: () => toast.success(t("files.uploaded", { count: files.length })),
      onError: (error) => setProblem(errorText(error)),
    });
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    send([...event.dataTransfer.files]);
  }

  return (
    <Panel
      title={t("files.title")}
      description={sqlite ? t("files.hintSqlite") : t("files.hintFiles")}
      actions={
        <>
          <input
            ref={inputRef}
            type="file"
            multiple={!sqlite}
            accept={accept.join(",")}
            className="hidden"
            aria-hidden
            tabIndex={-1}
            data-testid="sql-file-input"
            onChange={(event) => {
              const files = [...(event.target.files ?? [])];
              event.target.value = "";
              send(files);
            }}
          />
          <Button size="sm" loading={upload.isPending} onClick={() => inputRef.current?.click()}>
            <Upload className="size-3.5" aria-hidden />
            {sqlite && source.files.length > 0 ? t("files.replace") : t("files.upload")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div
          role="presentation"
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "rounded-md border border-dashed px-3 py-3 text-center text-xs text-ink-muted",
            dragging ? "border-accent bg-accent-soft" : "border-line",
          )}
        >
          {upload.isPending ? t("files.uploading") : t("files.drop")}
        </div>
        {problem ? <Alert tone="danger" title={problem} /> : null}
        {rows.length === 0 ? (
          <EmptyState icon={FileSpreadsheet} title={t("files.empty")} />
        ) : (
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
            {rows.map(({ file, row }) => (
              <li key={file.file_id} className="flex flex-col gap-3 px-3 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <FileSpreadsheet className="size-4 shrink-0 text-ink-subtle" aria-hidden />
                  <span className="min-w-0 truncate text-sm font-medium text-ink">
                    {file.filename}
                  </span>
                  <Badge>{file.format}</Badge>
                  <span className="text-xs text-ink-subtle tabular-nums">
                    {formatBytes(file.size_bytes)}
                  </span>
                  <Button
                    size="iconSm"
                    variant="dangerGhost"
                    className="ml-auto"
                    aria-label={t("files.remove", { name: file.filename })}
                    title={t("files.remove", { name: file.filename })}
                    onClick={() => setRemoving(file)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                  </Button>
                </div>
                {file.format === "sqlite" ? null : (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(12rem,1fr)_minmax(12rem,1fr)]">
                    <Field
                      label={t("files.table")}
                      hint={t("files.tableHint")}
                      error={tableProblem(row)}
                      required
                    >
                      <Input
                        className="font-mono"
                        value={row.table}
                        aria-label={`${file.filename} ${t("files.table")}`}
                        onChange={(event) => edit(file, { table: event.target.value })}
                      />
                    </Field>
                    <Field label={t("files.encoding")}>
                      <Select
                        aria-label={`${file.filename} ${t("files.encoding")}`}
                        value={row.encoding}
                        onValueChange={(value) => edit(file, { encoding: value as SqlEncoding })}
                        options={ENCODINGS.map((value) => ({
                          value,
                          label: t(`files.encodings.${value}`),
                        }))}
                      />
                    </Field>
                    {file.format === "xlsx" ? (
                      <Field label={t("files.sheet")} hint={t("files.sheetHint")}>
                        <Input
                          value={row.sheet}
                          aria-label={`${file.filename} ${t("files.sheet")}`}
                          onChange={(event) => edit(file, { sheet: event.target.value })}
                        />
                      </Field>
                    ) : null}
                    <div className="flex flex-col gap-2 sm:col-span-2 sm:flex-row sm:gap-6">
                      <Field inline label={t("files.header")}>
                        <Switch
                          checked={row.header}
                          aria-label={`${file.filename} ${t("files.header")}`}
                          onCheckedChange={(header) => edit(file, { header })}
                        />
                      </Field>
                      <Field inline label={t("files.allVarchar")} hint={t("files.allVarcharHint")}>
                        <Switch
                          checked={row.all_varchar}
                          aria-label={`${file.filename} ${t("files.allVarchar")}`}
                          onCheckedChange={(allVarchar) => edit(file, { all_varchar: allVarchar })}
                        />
                      </Field>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
        {patches.length > 0 ? (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <p className="mr-auto text-xs text-warn">{t("files.dirty")}</p>
            <Button size="sm" variant="ghost" onClick={() => setEdits({})}>
              {tc("actions.cancel")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={patch.isPending}
              disabled={invalid}
              onClick={() =>
                patch.mutate(
                  { files: patches },
                  {
                    onSuccess: () => {
                      setEdits({});
                      toast.success(t("files.saved"));
                    },
                    onError: (error) => toast.error(errorText(error)),
                  },
                )
              }
            >
              {t("files.save")}
            </Button>
          </div>
        ) : null}
      </div>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={t("files.removeTitle")}
        description={t("files.removeBody", { name: removing?.filename ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={removeFile.isPending}
        onConfirm={() => {
          if (!removing) return;
          const fileId = removing.file_id;
          removeFile.mutate(fileId, {
            onSuccess: () => {
              setEdits((current) => {
                const next = { ...current };
                delete next[fileId];
                return next;
              });
              setRemoving(null);
              toast.success(t("files.removed"));
            },
            onError: (error) => toast.error(errorText(error)),
          });
        }}
      />
    </Panel>
  );
}
