import {
  ChevronLeft,
  ChevronRight,
  Ellipsis,
  FileText,
  FolderOpen,
  Pencil,
  RefreshCw,
  ScanText,
  Search,
  Tags,
  Trash2,
  Upload,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import type {
  DisplayStatus,
  DocumentStatusResult,
  KbDocumentOut,
  KnowledgeBaseOut,
  MetadataFieldOut,
} from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/layout";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Select } from "@/components/ui/select";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Checkbox } from "@/components/ui/toggle";
import {
  useDeleteDocuments,
  useDocumentAction,
  useDocuments,
  useReindexDocument,
  useRenameDocument,
  type DocumentQuery,
} from "@/features/knowledge/api";
import {
  actionStates,
  DISPLAY_STATUSES,
  documentError,
  progressPercent,
  relativeBase,
  statusTone,
  type DocumentAction,
} from "@/features/knowledge/documentStatus";
import { skipMessage } from "@/features/knowledge/messages";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatNumber, formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { KbErrorAlert } from "./KbErrorAlert";
import { BulkMetadataDialog } from "./MetadataEditor";
import { NameDialog } from "./NameDialog";
import { DocumentLimitNote } from "./RateLimitNotice";

const PAGE_SIZE = 20;
const ACTIONS: readonly DocumentAction[] = [
  "enable",
  "disable",
  "archive",
  "unarchive",
  "pause",
  "resume",
  "retry",
];

type Pending =
  | { kind: "rename"; document: KbDocumentOut }
  | { kind: "reindex"; document: KbDocumentOut; mode?: "model" }
  | { kind: "delete"; documents: KbDocumentOut[] }
  | { kind: "metadata"; documents: KbDocumentOut[] };

function isPdf(document: KbDocumentOut): boolean {
  return document.mime === "application/pdf" || document.filename.toLowerCase().endsWith(".pdf");
}

export function StatusCell({ document }: { document: KbDocumentOut }) {
  const { t } = useTranslation("knowledge");
  const percent = progressPercent(document);
  const error = documentError(document);
  return (
    <div className="flex flex-col items-start gap-1">
      <Badge tone={statusTone(document.display_status)}>
        {t(`status.${document.display_status}`)}
      </Badge>
      {document.display_status === "indexing" ? (
        <div className="flex w-32 flex-col gap-0.5">
          <span className="text-xs text-ink-muted">
            {percent === null
              ? t(`stage.${document.indexing_status}`)
              : t("documents.progress", {
                  stage: t(`stage.${document.indexing_status}`),
                  percent,
                })}
          </span>
          {percent !== null ? (
            <div
              className="h-1 w-full overflow-hidden rounded-full bg-sunken"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              aria-label={document.name}
            >
              <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
            </div>
          ) : null}
        </div>
      ) : null}
      {error?.code === "rate_limited_daily" ? (
        <DocumentLimitNote document={document} />
      ) : error ? (
        <span className="max-w-64 truncate text-xs text-danger" title={error.message}>
          {error.message || error.code}
        </span>
      ) : null}
    </div>
  );
}

// 문서 표(설계 12.3 문서 탭): 상태 배지·진행률·일일 한도 안내, 필터, 일괄 조작. 진행 중 문서가 있을 때만
// 2초마다 다시 읽는다(useDocuments).
export function DocumentTable({
  kb,
  fields,
  onUpload,
  pollPaused = false,
}: {
  kb: KnowledgeBaseOut;
  fields: MetadataFieldOut[];
  onUpload: () => void;
  // 업로드 대화상자가 그 묶음의 진행을 읽는 동안은 표를 폴링하지 않는다(닫으면 다시 읽는다).
  pollPaused?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const navigate = useNavigate();
  const now = useNow();
  const [status, setStatus] = useState<DisplayStatus | "all">("all");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Map<string, KbDocumentOut>>(new Map());
  const [pending, setPending] = useState<Pending | null>(null);
  const [result, setResult] = useState<{
    action: DocumentAction;
    data: DocumentStatusResult;
  } | null>(null);
  const query: DocumentQuery = {
    status: status === "all" ? undefined : status,
    keyword: keyword.trim() || undefined,
    page,
    limit: PAGE_SIZE,
  };
  const documents = useDocuments(kb.id, query, kb.indexing_document_count, pollPaused);
  const action = useDocumentAction(kb.id);
  const reindex = useReindexDocument(kb.id);
  const rename = useRenameDocument(kb.id);
  const remove = useDeleteDocuments(kb.id);

  const items = documents.data?.items ?? [];
  // 고른 문서는 목록을 다시 읽어도 최신 상태로 본다(폴링 중 상태가 바뀐다).
  const fresh = new Map(items.map((item) => [item.id, item]));
  const chosen = [...selected.values()].map((item) => fresh.get(item.id) ?? item);
  const states = actionStates(chosen);
  const names = new Map(chosen.map((item) => [item.id, item.name]));
  const allOnPage = items.length > 0 && items.every((item) => selected.has(item.id));

  function toggle(document: KbDocumentOut, on: boolean) {
    setSelected((current) => {
      const next = new Map(current);
      if (on) next.set(document.id, document);
      else next.delete(document.id);
      return next;
    });
  }

  function toggleAll(on: boolean) {
    setSelected((current) => {
      const next = new Map(current);
      for (const item of items) {
        if (on) next.set(item.id, item);
        else next.delete(item.id);
      }
      return next;
    });
  }

  function run(kind: DocumentAction, ids: string[]) {
    setResult(null);
    action.mutate(
      { action: kind, ids },
      {
        onSuccess: (data) => {
          // 모두 건너뛰었으면 성공 알림 없이 건너뛴 사유만 보인다.
          if (data.items.length > 0) {
            toast.success(t(`documents.actionDone.${kind}`, { count: data.items.length }));
          }
          setResult(data.skipped.length > 0 ? { action: kind, data } : null);
        },
        onError: (error) => toast.error(errorText(error)),
      },
    );
  }

  let body;
  if (documents.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (documents.isError) {
    body = (
      <KbErrorAlert
        error={documents.error}
        title={t("documents.loadFailed")}
        onRetry={() => void documents.refetch()}
      />
    );
  } else if (items.length === 0) {
    body =
      status === "all" && !keyword.trim() ? (
        <EmptyState
          icon={FileText}
          title={t("documents.empty")}
          hint={t("documents.emptyHint")}
          action={
            <Button variant="primary" onClick={onUpload}>
              <Upload className="size-4" aria-hidden />
              {t("documents.upload")}
            </Button>
          }
        />
      ) : (
        <EmptyState icon={Search} title={t("documents.noMatch")} />
      );
  } else {
    const total = documents.data?.total ?? 0;
    const from = (page - 1) * PAGE_SIZE + 1;
    body = (
      <>
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-ink-muted">
              <tr className="border-b border-line">
                <th className="w-8 py-2 pl-3">
                  <Checkbox
                    checked={allOnPage}
                    aria-label={t("documents.selectAll")}
                    onCheckedChange={(checked) => toggleAll(checked === true)}
                  />
                </th>
                <th className="py-2 pr-3 font-medium">{t("documents.column.name")}</th>
                <th className="py-2 pr-3 font-medium">{t("documents.column.status")}</th>
                <th className="py-2 pr-3 text-right font-medium">{t("documents.column.chunks")}</th>
                <th className="py-2 pr-3 text-right font-medium">{t("documents.column.words")}</th>
                <th className="py-2 pr-3 text-right font-medium">{t("documents.column.hits")}</th>
                <th className="py-2 pr-3 font-medium">{t("documents.column.created")}</th>
                <th className="w-10 py-2 pr-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.map((document) => (
                <tr key={document.id} className="align-top">
                  <td className="py-2.5 pl-3">
                    <Checkbox
                      checked={selected.has(document.id)}
                      aria-label={t("documents.select", { name: document.name })}
                      onCheckedChange={(checked) => toggle(document, checked === true)}
                    />
                  </td>
                  <td className="max-w-80 py-2 pr-3">
                    <Link
                      to={`/knowledge/${kb.id}/documents/${document.id}`}
                      className="block truncate font-medium text-ink hover:underline"
                    >
                      {document.name}
                    </Link>
                    <span className="block truncate text-xs text-ink-subtle">
                      {t(`source.${document.data_source_type}`)}
                      {document.filename && document.filename !== document.name
                        ? ` · ${document.filename}`
                        : ""}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <StatusCell document={document} />
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {t("documents.chunksValue", {
                      done: document.completed_segments,
                      total: document.total_segments,
                    })}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {formatNumber(document.word_count)}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {formatNumber(document.hit_count)}
                  </td>
                  <td className="py-2 pr-3 text-xs whitespace-nowrap text-ink-muted">
                    {formatRelative(document.created_at, relativeBase(document.created_at, now))}
                  </td>
                  <td className="py-1.5 pr-2 text-right">
                    <Menu>
                      <MenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="iconSm"
                          aria-label={t("documents.menu", { name: document.name })}
                        >
                          <Ellipsis className="size-4" aria-hidden />
                        </Button>
                      </MenuTrigger>
                      <MenuContent align="end">
                        <MenuItem
                          onSelect={() => navigate(`/knowledge/${kb.id}/documents/${document.id}`)}
                        >
                          <FolderOpen className="size-4" aria-hidden />
                          {t("documents.open")}
                        </MenuItem>
                        <MenuItem onSelect={() => setPending({ kind: "rename", document })}>
                          <Pencil className="size-4" aria-hidden />
                          {t("documents.rename")}
                        </MenuItem>
                        <MenuItem
                          onSelect={() => setPending({ kind: "metadata", documents: [document] })}
                        >
                          <Tags className="size-4" aria-hidden />
                          {t("documents.editMetadata")}
                        </MenuItem>
                        <MenuItem onSelect={() => setPending({ kind: "reindex", document })}>
                          <RefreshCw className="size-4" aria-hidden />
                          {t("documents.reindex")}
                        </MenuItem>
                        {isPdf(document) ? (
                          <MenuItem
                            onSelect={() =>
                              setPending({ kind: "reindex", document, mode: "model" })
                            }
                          >
                            <ScanText className="size-4" aria-hidden />
                            {t("documents.modelRead")}
                          </MenuItem>
                        ) : null}
                        <MenuSeparator />
                        <MenuItem
                          tone="danger"
                          onSelect={() => setPending({ kind: "delete", documents: [document] })}
                        >
                          <Trash2 className="size-4" aria-hidden />
                          {t("documents.delete")}
                        </MenuItem>
                      </MenuContent>
                    </Menu>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2 flex items-center justify-end gap-2 text-xs text-ink-muted">
          <span>
            {t("documents.page", {
              from,
              to: from + items.length - 1,
              total,
            })}
          </span>
          <Button
            size="iconSm"
            variant="ghost"
            disabled={page <= 1}
            aria-label={t("documents.prev")}
            title={t("documents.prev")}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft className="size-4" aria-hidden />
          </Button>
          <Button
            size="iconSm"
            variant="ghost"
            disabled={!documents.data?.has_more}
            aria-label={t("documents.next")}
            title={t("documents.next")}
            onClick={() => setPage(page + 1)}
          >
            <ChevronRight className="size-4" aria-hidden />
          </Button>
        </div>
      </>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          aria-label={t("documents.filter")}
          className="w-36"
          value={status}
          onValueChange={(value) => {
            setStatus(value as DisplayStatus | "all");
            setPage(1);
          }}
          options={[
            { value: "all", label: t("documents.all") },
            ...DISPLAY_STATUSES.map((value) => ({ value, label: t(`status.${value}`) })),
          ]}
        />
        <div className="relative">
          <Search
            className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-subtle"
            aria-hidden
          />
          <Input
            type="search"
            value={keyword}
            placeholder={t("documents.search")}
            aria-label={t("documents.search")}
            className="w-60 pl-8"
            onChange={(event) => {
              setKeyword(event.target.value);
              setPage(1);
            }}
          />
        </div>
        <Button variant="primary" className="ml-auto" onClick={onUpload}>
          <Upload className="size-4" aria-hidden />
          {t("documents.upload")}
        </Button>
      </div>
      {chosen.length > 0 ? (
        <div
          role="toolbar"
          aria-label={t("documents.selected", { count: chosen.length })}
          className="flex flex-wrap items-center gap-1.5 rounded-md border border-line bg-sunken px-2.5 py-1.5"
        >
          <span className="mr-1 text-xs font-medium text-ink">
            {t("documents.selected", { count: chosen.length })}
          </span>
          {ACTIONS.map((kind) => {
            const state = states[kind];
            return (
              <Button
                key={kind}
                size="sm"
                variant="ghost"
                disabled={!state.enabled || action.isPending}
                title={state.reason ? t(`documents.actionBlocked.${state.reason}`) : undefined}
                onClick={() =>
                  run(
                    kind,
                    chosen.map((item) => item.id),
                  )
                }
              >
                {t(`documents.actions.${kind}`)}
              </Button>
            );
          })}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setPending({ kind: "metadata", documents: chosen })}
          >
            {t("documents.editMetadata")}
          </Button>
          <Button
            size="sm"
            variant="dangerGhost"
            onClick={() => setPending({ kind: "delete", documents: chosen })}
          >
            {t("documents.delete")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto"
            onClick={() => setSelected(new Map())}
          >
            {t("documents.clear")}
          </Button>
        </div>
      ) : null}
      {chosen.length > 0 && states.enable.reason === "indexing" ? (
        <p className="text-xs text-ink-muted">{t("documents.actionBlocked.indexing")}</p>
      ) : null}
      {result ? (
        <Alert
          tone="warn"
          title={t("documents.skippedTitle", { count: result.data.skipped.length })}
          action={
            <Button size="sm" variant="ghost" onClick={() => setResult(null)}>
              {tc("actions.close")}
            </Button>
          }
        >
          <ul className="mt-1 flex flex-col gap-0.5">
            {result.data.skipped.map((skip) => (
              <li key={skip.document_id}>
                <span className="font-medium text-ink">
                  {names.get(skip.document_id) ??
                    fresh.get(skip.document_id)?.name ??
                    t("documents.unknownDocument")}
                </span>
                : {skipMessage(skip, t)}
              </li>
            ))}
          </ul>
        </Alert>
      ) : null}
      {body}
      {pending?.kind === "rename" ? (
        <NameDialog
          key={pending.document.id}
          open
          onOpenChange={(open) => {
            if (!open) setPending(null);
          }}
          title={t("documents.renameTitle")}
          label={t("upload.name")}
          initial={pending.document.name}
          pending={rename.isPending}
          error={rename.error ? errorText(rename.error) : null}
          onSubmit={(name) =>
            rename.mutate(
              { docId: pending.document.id, name },
              {
                onSuccess: () => {
                  toast.success(t("documents.renamed"));
                  setPending(null);
                },
              },
            )
          }
        />
      ) : null}
      <ConfirmDialog
        open={pending?.kind === "reindex"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        tone="default"
        title={
          pending?.kind === "reindex" && pending.mode === "model"
            ? t("documents.modelReadTitle")
            : t("documents.reindexTitle")
        }
        description={
          pending?.kind === "reindex" && pending.mode === "model"
            ? t("documents.modelReadBody")
            : t("documents.reindexBody")
        }
        confirmLabel={t("documents.reindex")}
        pending={reindex.isPending}
        onConfirm={() => {
          if (pending?.kind !== "reindex") return;
          reindex.mutate(
            { docId: pending.document.id, mode: pending.mode },
            {
              onSuccess: () => {
                toast.success(t("documents.reindexed"));
                setPending(null);
              },
              onError: (error) => toast.error(errorText(error)),
            },
          );
        }}
      />
      <ConfirmDialog
        open={pending?.kind === "delete"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title={
          pending?.kind === "delete" && pending.documents.length > 1
            ? t("documents.deleteManyTitle", { count: pending.documents.length })
            : t("documents.deleteTitle")
        }
        description={
          pending?.kind === "delete" && pending.documents.length > 1
            ? t("documents.deleteManyBody")
            : t("documents.deleteBody", {
                name: pending?.kind === "delete" ? (pending.documents[0]?.name ?? "") : "",
              })
        }
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (pending?.kind !== "delete") return;
          const ids = pending.documents.map((item) => item.id);
          remove.mutate(ids, {
            onSuccess: () => {
              toast.success(t("documents.deleted"));
              setSelected((current) => {
                const next = new Map(current);
                for (const id of ids) next.delete(id);
                return next;
              });
              setPending(null);
            },
            onError: (error) => toast.error(errorText(error)),
          });
        }}
      />
      <BulkMetadataDialog
        kbId={kb.id}
        documentIds={pending?.kind === "metadata" ? pending.documents.map((item) => item.id) : []}
        fields={fields}
        open={pending?.kind === "metadata"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
      />
    </div>
  );
}
