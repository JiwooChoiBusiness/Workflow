import { ChevronLeft, ChevronRight, Layers, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { KbDocumentOut, KnowledgeBaseOut, SegmentOut } from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/layout";
import { Select } from "@/components/ui/select";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import {
  useCreateSegment,
  useDeleteSegment,
  usePatchSegment,
  useSegments,
  useSegmentsFollowDocument,
  type SegmentQuery,
} from "@/features/knowledge/api";
import { isInProgress } from "@/features/knowledge/documentStatus";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { ChildChunkList } from "./ChildChunkList";
import { KbErrorAlert } from "./KbErrorAlert";
import { SegmentEditor } from "./SegmentEditor";

const PAGE_SIZE = 50;
const STATUS_TONE: Record<SegmentOut["status"], BadgeTone> = {
  waiting: "neutral",
  indexing: "info",
  completed: "ok",
  error: "danger",
};
const STATE_TONE: Record<string, BadgeTone> = {
  ok: "ok",
  pending: "neutral",
  skip: "neutral",
  error: "danger",
};
type IndexState = "ok" | "pending" | "skip" | "error";

function indexState(value: unknown): IndexState {
  return value === "ok" || value === "pending" || value === "skip" || value === "error"
    ? value
    : "pending";
}

type EnabledFilter = "all" | "on" | "off";
type Editing = { segment?: SegmentOut } | null;

// 문서 화면의 청크 목록(설계 12.3): 검색·켜기·끄기·고치기·더하기·지우기. Parent-child는 부모 카드 안에 자식
// 목록, Q&A는 질문·답 두 칸이다. 색인 중이거나 보관한 문서는 고칠 수 없다.
export function SegmentList({ kb, document }: { kb: KnowledgeBaseOut; document: KbDocumentOut }) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const [keyword, setKeyword] = useState("");
  const [enabled, setEnabled] = useState<EnabledFilter>("all");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Editing>(null);
  const [deleting, setDeleting] = useState<SegmentOut | null>(null);
  const busy = isInProgress(document);
  const readOnly = busy || document.archived;
  const query: SegmentQuery = {
    keyword: keyword.trim() || undefined,
    enabled: enabled === "all" ? undefined : enabled === "on",
    page,
    limit: PAGE_SIZE,
  };
  const segments = useSegments(kb.id, document.id, query);
  useSegmentsFollowDocument(kb.id, document);
  const create = useCreateSegment(kb.id, document.id);
  const patch = usePatchSegment(kb.id, document.id);
  const remove = useDeleteSegment(kb.id, document.id);
  const qa = kb.doc_form === "qa_model";
  const hierarchical = kb.doc_form === "hierarchical_model";

  let body;
  if (segments.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (segments.isError) {
    body = (
      <KbErrorAlert
        error={segments.error}
        title={t("segments.loadFailed")}
        onRetry={() => void segments.refetch()}
      />
    );
  } else if (segments.data.items.length === 0) {
    body = <EmptyState icon={keyword ? Search : Layers} title={t("segments.empty")} />;
  } else {
    const from = (page - 1) * PAGE_SIZE + 1;
    body = (
      <>
        <ol className="flex flex-col gap-2">
          {segments.data.items.map((segment) => {
            const state = segment.index_state as Record<string, unknown>;
            const fts = indexState(state.fts);
            const vector = indexState(state.vector);
            return (
              <li
                key={segment.id}
                className={
                  "flex flex-col gap-2 rounded-lg border bg-surface p-3 " +
                  (segment.enabled ? "border-line" : "border-dashed border-line opacity-70")
                }
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs font-medium text-ink">
                    {t("segments.position", { position: segment.position })}
                  </span>
                  <Badge tone={STATUS_TONE[segment.status]}>
                    {t(`segments.status.${segment.status}`)}
                  </Badge>
                  {hierarchical ? null : (
                    <>
                      <Badge tone={STATE_TONE[fts]}>
                        {t("segments.state.fts")} {t(`segments.state.${fts}`)}
                      </Badge>
                      {kb.indexing_technique === "high_quality" ? (
                        <Badge tone={STATE_TONE[vector]}>
                          {t("segments.state.vector")} {t(`segments.state.${vector}`)}
                        </Badge>
                      ) : null}
                    </>
                  )}
                  {segment.edited ? <Badge tone="info">{t("segments.edited")}</Badge> : null}
                  {segment.created_by === "customized" ? (
                    <Badge tone="info">{t("segments.customized")}</Badge>
                  ) : null}
                  <span className="text-xs text-ink-subtle">
                    {t("segments.meta", {
                      tokens: segment.tokens,
                      words: segment.word_count,
                      hits: segment.hit_count,
                    })}
                  </span>
                  <div className="ml-auto flex items-center gap-1">
                    <Field
                      label={t("segments.enabled")}
                      inline
                      className="items-center text-xs"
                      hint={segment.status === "completed" ? undefined : t("segments.notCompleted")}
                    >
                      <Switch
                        checked={segment.enabled}
                        aria-label={t("segments.enableLabel", { position: segment.position })}
                        disabled={readOnly || segment.status !== "completed" || patch.isPending}
                        onCheckedChange={(checked) =>
                          patch.mutate(
                            { segmentId: segment.id, body: { enabled: checked } },
                            {
                              onSuccess: () =>
                                toast.success(
                                  checked ? t("segments.enabledOn") : t("segments.enabledOff"),
                                ),
                              onError: (error) => toast.error(errorText(error)),
                            },
                          )
                        }
                      />
                    </Field>
                    {readOnly ? null : (
                      <>
                        <Button
                          size="iconSm"
                          variant="ghost"
                          aria-label={t("segments.editTitle", { position: segment.position })}
                          title={t("segments.edit")}
                          onClick={() => setEditing({ segment })}
                        >
                          <Pencil className="size-3.5" aria-hidden />
                        </Button>
                        <Button
                          size="iconSm"
                          variant="dangerGhost"
                          aria-label={`${t("segments.delete")} ${t("segments.position", { position: segment.position })}`}
                          title={t("segments.delete")}
                          onClick={() => setDeleting(segment)}
                        >
                          <Trash2 className="size-3.5" aria-hidden />
                        </Button>
                      </>
                    )}
                  </div>
                </div>
                {qa ? (
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div>
                      <p className="text-xs font-medium text-ink-muted">{t("segments.question")}</p>
                      <p className="text-sm whitespace-pre-wrap text-ink">{segment.content}</p>
                    </div>
                    <div>
                      <p className="text-xs font-medium text-ink-muted">{t("segments.answer")}</p>
                      <p className="text-sm whitespace-pre-wrap text-ink">{segment.answer}</p>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm whitespace-pre-wrap text-ink">{segment.content}</p>
                )}
                {hierarchical ? (
                  <ChildChunkList
                    kbId={kb.id}
                    documentId={document.id}
                    segmentId={segment.id}
                    chunks={segment.child_chunks ?? []}
                    readOnly={readOnly}
                  />
                ) : null}
              </li>
            );
          })}
        </ol>
        <div className="flex items-center justify-end gap-2 text-xs text-ink-muted">
          <span>
            {t("segments.page", {
              from,
              to: from + segments.data.items.length - 1,
              total: segments.data.total,
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
            disabled={!segments.data.has_more}
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

  const target = editing?.segment;
  const saving = target ? patch : create;

  return (
    <section aria-label={t("segments.title")} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-2 text-sm font-semibold text-ink">{t("segments.title")}</h2>
        <div className="relative">
          <Search
            className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-subtle"
            aria-hidden
          />
          <Input
            type="search"
            value={keyword}
            placeholder={t("segments.search")}
            aria-label={t("segments.search")}
            className="w-56 pl-8"
            onChange={(event) => {
              setKeyword(event.target.value);
              setPage(1);
            }}
          />
        </div>
        <Select
          aria-label={t("segments.enabledFilter")}
          className="w-32"
          value={enabled}
          onValueChange={(value) => {
            setEnabled(value as EnabledFilter);
            setPage(1);
          }}
          options={[
            { value: "all", label: t("segments.enabledAll") },
            { value: "on", label: t("segments.enabledOn") },
            { value: "off", label: t("segments.enabledOff") },
          ]}
        />
        <Button className="ml-auto" disabled={readOnly} onClick={() => setEditing({})}>
          <Plus className="size-4" aria-hidden />
          {t("segments.add")}
        </Button>
      </div>
      {body}
      {editing ? (
        <SegmentEditor
          key={target?.id ?? "new"}
          open
          onOpenChange={(open) => {
            if (!open) {
              setEditing(null);
              create.reset();
              patch.reset();
            }
          }}
          title={
            target
              ? t("segments.editTitle", { position: target.position })
              : t("segments.createTitle")
          }
          hint={t("segments.editHint")}
          initialContent={target?.content}
          initialAnswer={target?.answer}
          qa={qa}
          pending={saving.isPending}
          error={saving.error ? errorText(saving.error) : null}
          onSave={(content, answer) => {
            const done = {
              onSuccess: () => {
                toast.success(target ? t("segments.saved") : t("segments.created"));
                setEditing(null);
              },
            };
            if (target) {
              patch.mutate(
                { segmentId: target.id, body: { content, ...(qa ? { answer } : {}) } },
                done,
              );
            } else {
              create.mutate({ content, ...(qa ? { answer } : {}) }, done);
            }
          }}
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("segments.deleteTitle")}
        description={t("segments.deleteBody", { position: deleting?.position ?? 0 })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("segments.deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorText(error)),
          });
        }}
      />
    </section>
  );
}
