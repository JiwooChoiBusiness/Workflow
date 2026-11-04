import { Database, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { SqlSourceSummary } from "@/api/types";
import { KnowledgeSections } from "@/components/knowledge/KnowledgeSections";
import { CreateSqlSourceDialog } from "@/components/sql/CreateSqlSourceDialog";
import { SqlErrorAlert } from "@/components/sql/SqlErrorAlert";
import { SqlSourceCard } from "@/components/sql/SqlSourceCard";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { EmptyState, PageHeader } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import "@/features/knowledge/i18n";
import { useDeleteSqlSource, useSqlSources } from "@/features/sql/api";
import "@/features/sql/i18n";
import { useSqlError } from "@/features/sql/useSqlError";

// 데이터 소스 목록(설계 12.3 /knowledge/sql-sources). 상단 탭으로 지식 베이스 목록과 오간다.
export default function SqlSourcesPage() {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const errorText = useSqlError();
  const sources = useSqlSources();
  const remove = useDeleteSqlSource();
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<SqlSourceSummary | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (sources.data ?? []).filter(
      (source) =>
        !needle ||
        `${source.name} ${source.description} ${source.slug}`.toLowerCase().includes(needle),
    );
  }, [sources.data, query]);

  const newButton = (
    <Button variant="primary" onClick={() => setCreating(true)}>
      <Plus className="size-4" aria-hidden />
      {t("list.new")}
    </Button>
  );

  let body;
  if (sources.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (sources.isError) {
    body = (
      <SqlErrorAlert
        error={sources.error}
        title={t("list.loadFailed")}
        onRetry={() => void sources.refetch()}
      />
    );
  } else if (sources.data.length === 0) {
    body = (
      <EmptyState
        icon={Database}
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
        {filtered.map((source) => (
          <SqlSourceCard key={source.id} source={source} onDelete={setDeleting} />
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
      {body}
      <CreateSqlSourceDialog open={creating} onOpenChange={setCreating} />
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
