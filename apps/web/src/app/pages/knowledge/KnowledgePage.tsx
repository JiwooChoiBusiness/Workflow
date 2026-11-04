import { BookOpen, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { KnowledgeBaseOut } from "@/api/types";
import { CreateKbDialog } from "@/components/knowledge/CreateKbDialog";
import { IndexUnavailableNotice } from "@/components/knowledge/IndexUnavailableNotice";
import { KbCard } from "@/components/knowledge/KbCard";
import { KbErrorAlert } from "@/components/knowledge/KbErrorAlert";
import { KnowledgeSections } from "@/components/knowledge/KnowledgeSections";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { EmptyState, PageHeader } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  useDeleteKnowledgeBase,
  useKnowledgeBases,
  useKnowledgeDefaults,
} from "@/features/knowledge/api";
import "@/features/knowledge/i18n";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";

// 지식 베이스 목록(설계 12.3 /knowledge). 상단 탭으로 데이터 소스 목록(Text2SQL)과 오간다.
export default function KnowledgePage() {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const bases = useKnowledgeBases();
  const defaults = useKnowledgeDefaults();
  const remove = useDeleteKnowledgeBase();
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<KnowledgeBaseOut | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (bases.data ?? []).filter(
      (kb) => !needle || `${kb.name} ${kb.description} ${kb.slug}`.toLowerCase().includes(needle),
    );
  }, [bases.data, query]);

  const newButton = (
    <Button variant="primary" onClick={() => setCreating(true)}>
      <Plus className="size-4" aria-hidden />
      {t("list.new")}
    </Button>
  );

  let body;
  if (bases.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (bases.isError) {
    body = (
      <KbErrorAlert
        error={bases.error}
        title={t("list.loadFailed")}
        onRetry={() => void bases.refetch()}
      />
    );
  } else if (bases.data.length === 0) {
    body = (
      <EmptyState
        icon={BookOpen}
        title={t("list.empty")}
        hint={t("list.emptyHint")}
        action={newButton}
      />
    );
  } else if (filtered.length === 0) {
    body = <EmptyState icon={Search} title={t("list.noMatch")} />;
  } else {
    body = (
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] gap-3">
        {filtered.map((kb) => (
          <KbCard key={kb.id} kb={kb} onDelete={setDeleting} />
        ))}
      </ul>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <KnowledgeSections />
      <PageHeader
        title={t("list.title")}
        description={t("list.description")}
        actions={
          <>
            <div className="relative">
              <Search
                className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-subtle"
                aria-hidden
              />
              <Input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("list.search")}
                aria-label={t("list.search")}
                className="w-64 pl-8"
              />
            </div>
            {newButton}
          </>
        }
      />
      {defaults.data && !defaults.data.index_available ? (
        <IndexUnavailableNotice reason={defaults.data.index_error} className="mb-4" />
      ) : null}
      {body}
      <CreateKbDialog open={creating} onOpenChange={setCreating} defaults={defaults.data} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("list.deleteTitle")}
        description={t("list.deleteBody", { name: deleting?.name ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("list.deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorText(error)),
          });
        }}
      />
    </div>
  );
}
