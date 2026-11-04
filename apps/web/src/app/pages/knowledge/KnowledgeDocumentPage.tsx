import { Download, Pencil, RefreshCw, ScanText, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router";
import type { KbDocumentOut } from "@/api/types";
import { StatusCell } from "@/components/knowledge/DocumentTable";
import { KbErrorAlert } from "@/components/knowledge/KbErrorAlert";
import { MetadataEditor } from "@/components/knowledge/MetadataEditor";
import { NameDialog } from "@/components/knowledge/NameDialog";
import { SegmentList } from "@/components/knowledge/SegmentList";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { KeyValueList, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  useDeleteDocuments,
  useDocument,
  useKnowledgeBase,
  useMetadataFields,
  useReindexDocument,
  useRenameDocument,
} from "@/features/knowledge/api";
import { documentError, isInProgress } from "@/features/knowledge/documentStatus";
import type { KT } from "@/features/knowledge/messages";
import "@/features/knowledge/i18n";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatBytes, formatDateTime, formatNumber } from "@/lib/format";

function isPdf(document: KbDocumentOut): boolean {
  return document.mime === "application/pdf" || document.filename.toLowerCase().endsWith(".pdf");
}

function extractText(document: KbDocumentOut, t: KT) {
  const extract = document.extract;
  if (!extract) return "-";
  const parts: string[] = [];
  if (extract.mode === "model") parts.push(t("document.extractModel"));
  if (typeof extract.pages === "number") parts.push(t("document.pages", { count: extract.pages }));
  if (typeof extract.chars === "number") {
    parts.push(t("document.chars", { count: formatNumber(extract.chars) }));
  }
  if (extract.truncated === true) parts.push(t("document.truncated"));
  return parts.join(" · ") || "-";
}

type Pending = "rename" | "reindex" | "model" | "delete" | null;

// 문서 화면(설계 12.3 /knowledge/:id/documents/:docId): 문서 정보와 상태, 메타데이터 값, 청크 목록, 다시 색인.
export default function KnowledgeDocumentPage() {
  const { id = "", docId = "" } = useParams();
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const errorText = useKnowledgeError();
  const kb = useKnowledgeBase(id);
  const document = useDocument(id, docId);
  const fields = useMetadataFields(id);
  const reindex = useReindexDocument(id);
  const rename = useRenameDocument(id);
  const remove = useDeleteDocuments(id);
  const [pending, setPending] = useState<Pending>(null);

  if (kb.isPending || document.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (kb.isError || document.isError) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-6">
        <KbErrorAlert
          error={kb.error ?? document.error}
          title={t("document.loadFailed")}
          onRetry={() => {
            void kb.refetch();
            void document.refetch();
          }}
        />
      </div>
    );
  }
  const base = kb.data;
  const doc = document.data;
  const error = documentError(doc);
  const busy = isInProgress(doc);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 px-6 py-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-ink">{doc.name}</h1>
          <div className="mt-1.5">
            <StatusCell document={doc} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* 서버가 늘 attachment로 주므로 브라우저가 열지 않고 내려받는다. */}
          <Button asChild size="sm">
            <a href={`/api/console/knowledge/${base.id}/documents/${doc.id}/file`} download>
              <Download className="size-3.5" aria-hidden />
              {t("document.download")}
            </a>
          </Button>
          <Button size="sm" onClick={() => setPending("rename")} disabled={doc.archived}>
            <Pencil className="size-3.5" aria-hidden />
            {t("documents.rename")}
          </Button>
          <Button size="sm" onClick={() => setPending("reindex")}>
            <RefreshCw className="size-3.5" aria-hidden />
            {t("documents.reindex")}
          </Button>
          {isPdf(doc) ? (
            <Button size="sm" onClick={() => setPending("model")}>
              <ScanText className="size-3.5" aria-hidden />
              {t("documents.modelRead")}
            </Button>
          ) : null}
          <Button size="sm" variant="dangerGhost" onClick={() => setPending("delete")}>
            <Trash2 className="size-3.5" aria-hidden />
            {t("documents.delete")}
          </Button>
        </div>
      </div>
      {error && error.code !== "rate_limited_daily" ? (
        <Alert tone="danger" title={error.message || error.code} />
      ) : null}
      {doc.archived ? <Alert tone="info" title={t("document.archivedNote")} /> : null}
      {!doc.archived && !doc.enabled ? (
        <Alert tone="info" title={t("document.disabledNote")} />
      ) : null}
      {busy ? <Alert tone="info" title={t("document.indexingNote")} /> : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title={t("document.info")}>
          <KeyValueList
            items={[
              {
                label: t("document.filename"),
                value: <span className="font-mono text-xs">{doc.filename}</span>,
              },
              { label: t("document.source"), value: t(`source.${doc.data_source_type}`) },
              { label: t("document.size"), value: formatBytes(doc.size_bytes) },
              {
                label: t("document.chunks"),
                value: t("documents.chunksValue", {
                  done: doc.completed_segments,
                  total: doc.total_segments,
                }),
              },
              { label: t("document.words"), value: formatNumber(doc.word_count) },
              { label: t("document.tokens"), value: formatNumber(doc.tokens) },
              { label: t("document.hits"), value: formatNumber(doc.hit_count) },
              { label: t("document.extract"), value: extractText(doc, t) },
              { label: t("document.created"), value: formatDateTime(doc.created_at) },
              { label: t("document.completed"), value: formatDateTime(doc.completed_at) },
              {
                label: t("document.batch"),
                value: <span className="font-mono text-xs">{doc.batch}</span>,
              },
            ]}
          />
        </Panel>
        <Panel title={t("metadata.values")}>
          {fields.data ? (
            <MetadataEditor
              kbId={base.id}
              documentId={doc.id}
              fields={fields.data.items}
              current={doc.doc_metadata ?? []}
            />
          ) : (
            <PageSpinner label={tc("state.loading")} />
          )}
        </Panel>
      </div>
      <SegmentList kb={base} document={doc} />
      {pending === "rename" ? (
        <NameDialog
          open
          onOpenChange={(open) => {
            if (!open) setPending(null);
          }}
          title={t("documents.renameTitle")}
          label={t("upload.name")}
          initial={doc.name}
          pending={rename.isPending}
          error={rename.error ? errorText(rename.error) : null}
          onSubmit={(name) =>
            rename.mutate(
              { docId: doc.id, name },
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
        open={pending === "reindex" || pending === "model"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        tone="default"
        title={pending === "model" ? t("documents.modelReadTitle") : t("documents.reindexTitle")}
        description={
          pending === "model" ? t("documents.modelReadBody") : t("documents.reindexBody")
        }
        confirmLabel={t("documents.reindex")}
        pending={reindex.isPending}
        onConfirm={() =>
          reindex.mutate(
            { docId: doc.id, mode: pending === "model" ? "model" : undefined },
            {
              onSuccess: () => {
                toast.success(t("documents.reindexed"));
                setPending(null);
              },
              onError: (failure) => toast.error(errorText(failure)),
            },
          )
        }
      />
      <ConfirmDialog
        open={pending === "delete"}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title={t("documents.deleteTitle")}
        description={t("documents.deleteBody", { name: doc.name })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate([doc.id], {
            onSuccess: () => {
              toast.success(t("documents.deleted"));
              navigate(`/knowledge/${base.id}`, { replace: true });
            },
            onError: (failure) => toast.error(errorText(failure)),
          })
        }
      />
    </div>
  );
}
