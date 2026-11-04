import { Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ChildChunkOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { useDeleteChild, useSaveChild } from "@/features/knowledge/api";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { SegmentEditor } from "./SegmentEditor";

type Editing = { child?: ChildChunkOut } | null;

// 부모 청크 카드 안의 자식 청크 목록(Parent-child, 설계 12.3). 자식이 검색 단위라 고치면 바로 다시 임베딩한다.
export function ChildChunkList({
  kbId,
  documentId,
  segmentId,
  chunks,
  readOnly,
}: {
  kbId: string;
  documentId: string;
  segmentId: string;
  chunks: ChildChunkOut[];
  readOnly: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const save = useSaveChild(kbId, documentId);
  const remove = useDeleteChild(kbId, documentId);
  const [editing, setEditing] = useState<Editing>(null);
  const [deleting, setDeleting] = useState<ChildChunkOut | null>(null);
  return (
    <div className="flex flex-col gap-1.5 border-l-2 border-line pl-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium text-ink-muted">
          {t("segments.children", { count: chunks.length })}
        </span>
        {readOnly ? null : (
          <Button size="sm" variant="ghost" onClick={() => setEditing({})}>
            <Plus className="size-3.5" aria-hidden />
            {t("segments.addChild")}
          </Button>
        )}
      </div>
      <ol className="flex flex-col gap-1">
        {chunks.map((child) => (
          <li key={child.id} className="group flex items-start gap-2 text-xs">
            <span className="shrink-0 font-mono text-ink-subtle">
              {t("segments.childPosition", { position: child.position })}
            </span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap text-ink">{child.content}</span>
            <span className="shrink-0 text-ink-subtle">{child.tokens}</span>
            {readOnly ? null : (
              <span className="flex shrink-0 gap-0.5">
                <Button
                  size="iconSm"
                  variant="ghost"
                  aria-label={t("segments.childEditTitle", { position: child.position })}
                  title={t("segments.edit")}
                  onClick={() => setEditing({ child })}
                >
                  <Pencil className="size-3" aria-hidden />
                </Button>
                <Button
                  size="iconSm"
                  variant="dangerGhost"
                  aria-label={t("segments.childDeleteTitle")}
                  title={t("segments.delete")}
                  onClick={() => setDeleting(child)}
                >
                  <Trash2 className="size-3" aria-hidden />
                </Button>
              </span>
            )}
          </li>
        ))}
      </ol>
      {editing ? (
        <SegmentEditor
          key={editing.child?.id ?? "new"}
          open
          onOpenChange={(open) => {
            if (!open) {
              setEditing(null);
              save.reset();
            }
          }}
          title={
            editing.child
              ? t("segments.childEditTitle", { position: editing.child.position })
              : t("segments.childCreateTitle")
          }
          hint={t("segments.editHint")}
          initialContent={editing.child?.content}
          pending={save.isPending}
          error={save.error ? errorText(save.error) : null}
          onSave={(content) =>
            save.mutate(
              { segmentId, childId: editing.child?.id, content },
              {
                onSuccess: () => {
                  toast.success(editing.child ? t("segments.saved") : t("segments.created"));
                  setEditing(null);
                },
              },
            )
          }
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("segments.childDeleteTitle")}
        description={t("segments.childDeleteBody", { position: deleting?.position ?? 0 })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(
            { segmentId, childId: deleting.id },
            {
              onSuccess: () => {
                toast.success(t("segments.deleted"));
                setDeleting(null);
              },
              onError: (error) => toast.error(errorText(error)),
            },
          );
        }}
      />
    </div>
  );
}
