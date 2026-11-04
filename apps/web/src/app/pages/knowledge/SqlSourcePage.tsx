import { Database } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useParams, useSearchParams } from "react-router";
import type { SqlSourceOut } from "@/api/types";
import { AnnotationEditor } from "@/components/sql/AnnotationEditor";
import { PrepareStatus } from "@/components/sql/PrepareStatus";
import { RuntimeStatus } from "@/components/sql/RuntimeStatus";
import { SourceFilesEditor } from "@/components/sql/SourceFilesEditor";
import { SqlErrorAlert } from "@/components/sql/SqlErrorAlert";
import { SqlSourceSettings } from "@/components/sql/SqlSourceSettings";
import { SqlTestPanel } from "@/components/sql/SqlTestPanel";
import { Badge } from "@/components/ui/badge";
import { PageSpinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import "@/features/knowledge/i18n";
import { useSqlRuntime, useSqlSource } from "@/features/sql/api";
import "@/features/sql/i18n";

const TABS = ["files", "schema", "test", "settings"] as const;
type TabKey = (typeof TABS)[number];

function isTab(value: string | null): value is TabKey {
  return TABS.includes(value as TabKey);
}

function Header({ source }: { source: SqlSourceOut }) {
  const { t } = useTranslation("sql");
  const { t: tk } = useTranslation("knowledge");
  return (
    <div className="flex items-start gap-3">
      <span className="grid size-9 shrink-0 place-items-center rounded-md bg-accent-soft text-accent">
        <Database className="size-4" aria-hidden />
      </span>
      <div className="min-w-0">
        <h1 className="truncate text-xl font-semibold text-ink">{source.name}</h1>
        {source.description ? (
          <p className="mt-0.5 text-sm text-ink-muted">{source.description}</p>
        ) : null}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Badge>{t(`kind.${source.kind}`)}</Badge>
          {/* SQLite 소스는 종류 이름이 곧 방언이다. CSV·엑셀만 질의 방언(DuckDB)을 따로 보인다. */}
          {source.dialect === "duckdb" ? <Badge>DuckDB</Badge> : null}
          <Badge tone={source.approval === "always" ? "info" : "neutral"}>
            {t(`approval.${source.approval}`)}
          </Badge>
          <Badge tone={source.data_policy.key_tier === "paid" ? "warn" : "neutral"}>
            {tk(`tier.${source.data_policy.key_tier}`)}
          </Badge>
          <span className="font-mono text-xs text-ink-subtle">{source.slug}</span>
        </div>
      </div>
    </div>
  );
}

// 데이터 소스 화면(설계 12.3 /knowledge/sql-sources/:id). 탭은 주소의 ?tab=이다.
export default function SqlSourcePage() {
  const { id = "" } = useParams();
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: TabKey = isTab(params.get("tab")) ? (params.get("tab") as TabKey) : "files";
  const source = useSqlSource(id);
  const runtime = useSqlRuntime(source.data?.kind === "files");

  if (source.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (source.isError) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <SqlErrorAlert
          error={source.error}
          title={t("detail.loadFailed")}
          onRetry={() => void source.refetch()}
        />
      </div>
    );
  }
  const data = source.data;
  const files = data.kind === "files";
  const runtimeReady = !files ? true : runtime.data ? runtime.data.state === "ready" : null;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-6">
      <Header source={data} />
      <PrepareStatus source={data} runtimeReady={runtimeReady} />
      {files ? <RuntimeStatus /> : null}
      <Tabs
        value={tab}
        onValueChange={(value) => {
          const next = new URLSearchParams(params);
          if (value === "files") next.delete("tab");
          else next.set("tab", value);
          setParams(next, { replace: true });
        }}
      >
        <TabsList aria-label={t("detail.tabsLabel")}>
          {TABS.map((key) => (
            <TabsTrigger key={key} value={key}>
              {t(`detail.tabs.${key}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="files" className="pt-4">
          <SourceFilesEditor source={data} />
        </TabsContent>
        <TabsContent value="schema" className="pt-4">
          <AnnotationEditor source={data} />
        </TabsContent>
        <TabsContent value="test" className="pt-4">
          <SqlTestPanel source={data} />
        </TabsContent>
        <TabsContent value="settings" className="pt-4">
          {/* 칸은 저장에 성공할 때만 다시 채운다. 다른 데이터 소스로 넘어가면 새로 그린다. */}
          <SqlSourceSettings key={data.id} source={data} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
