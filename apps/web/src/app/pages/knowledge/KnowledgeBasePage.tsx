import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useSearchParams } from "react-router";
import type { KnowledgeBaseOut, KnowledgeBaseStats } from "@/api/types";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { DocumentTable } from "@/components/knowledge/DocumentTable";
import { EvalPanel } from "@/components/knowledge/EvalPanel";
import { HitTestPanel } from "@/components/knowledge/HitTestPanel";
import { IndexUnavailableNotice } from "@/components/knowledge/IndexUnavailableNotice";
import { KbApiPanel } from "@/components/knowledge/KbApiPanel";
import { KbErrorAlert } from "@/components/knowledge/KbErrorAlert";
import { KbSettingsPanel } from "@/components/knowledge/KbSettingsPanel";
import { MetadataFieldsPanel } from "@/components/knowledge/MetadataFieldsPanel";
import { RateLimitNotice } from "@/components/knowledge/RateLimitNotice";
import { UploadDialog } from "@/components/knowledge/UploadDialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { PageSpinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  useKnowledgeBase,
  useKnowledgeDefaults,
  useKnowledgeStats,
  useMetadataFields,
  useRefreshWhenSettled,
} from "@/features/knowledge/api";
import "@/features/knowledge/i18n";
import { formatBytes, formatNumber } from "@/lib/format";

const TABS = ["documents", "hitTest", "metadata", "eval", "settings", "api"] as const;
type TabKey = (typeof TABS)[number];

function isTab(value: string | null): value is TabKey {
  return TABS.includes(value as TabKey);
}

function StatsBar({ stats }: { stats: KnowledgeBaseStats }) {
  const { t } = useTranslation("knowledge");
  const vectors =
    stats.indexing_technique === "economy"
      ? t("stats.vectorsNone")
      : stats.vector_ratio === null || stats.vector_ratio === undefined
        ? t("stats.unknown")
        : t("stats.vectorsValue", {
            percent: Math.floor(stats.vector_ratio * 100),
            count: stats.vector_count ?? 0,
          });
  const active = stats.documents.queuing + stats.documents.indexing;
  const items = [
    {
      label: t("stats.documents"),
      value: (
        <span className="flex flex-wrap items-center gap-1.5">
          {t("stats.documentsValue", { total: formatNumber(stats.documents.total) })}
          {active > 0 ? (
            <Badge tone="info">{t("stats.inProgress", { count: active })}</Badge>
          ) : null}
          {stats.documents.error > 0 ? (
            <Badge tone="danger">{t("stats.errors", { count: stats.documents.error })}</Badge>
          ) : null}
        </span>
      ),
    },
    { label: t("stats.segments"), value: formatNumber(stats.segment_count) },
    {
      label: t("stats.units"),
      value:
        stats.unit_count === null || stats.unit_count === undefined
          ? t("stats.unknown")
          : formatNumber(stats.unit_count),
      hint: t("stats.unitsHint"),
    },
    { label: t("stats.vectors"), value: vectors },
    {
      label: t("stats.indexFile"),
      value: formatBytes(stats.index_bytes),
      hint: t("stats.indexFileHint"),
    },
    {
      label: t("stats.tokens"),
      value: formatNumber(stats.embedded_tokens_estimated),
      hint: t("stats.tokensHint"),
    },
  ];
  const pending = stats.pending_vector_units ?? 0;
  return (
    <div className="flex flex-col gap-1.5">
      <dl
        aria-label={t("stats.label")}
        className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-line bg-surface px-4 py-3 sm:grid-cols-3 lg:grid-cols-6"
      >
        {items.map((item) => (
          <div key={item.label} className="min-w-0" title={item.hint}>
            <dt className="text-xs text-ink-muted">{item.label}</dt>
            <dd className="text-sm font-medium text-ink">{item.value}</dd>
          </div>
        ))}
      </dl>
      {active > 0 && pending > 0 && stats.estimated_minutes !== null ? (
        <p className="text-xs text-ink-muted" title={t("stats.pendingHint")}>
          {t("stats.pending", { count: pending, minutes: stats.estimated_minutes })} ·{" "}
          {t("stats.pendingHint")}
        </p>
      ) : null}
    </div>
  );
}

function Header({ kb }: { kb: KnowledgeBaseOut }) {
  const { t } = useTranslation("knowledge");
  return (
    <div className="flex items-start gap-3">
      <AgentIcon icon={kb.icon} name={kb.name} />
      <div className="min-w-0">
        <h1 className="truncate text-xl font-semibold text-ink">{kb.name}</h1>
        {kb.description ? <p className="mt-0.5 text-sm text-ink-muted">{kb.description}</p> : null}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Badge tone={kb.indexing_technique === "high_quality" ? "accent" : "neutral"}>
            {t(`technique.${kb.indexing_technique}`)}
          </Badge>
          <Badge>{t(`docForm.${kb.doc_form}`)}</Badge>
          {kb.embedding ? (
            <Badge>
              {kb.embedding.model} · {kb.embedding.dimensions}
            </Badge>
          ) : null}
          <Badge tone={kb.data_policy.key_tier === "paid" ? "warn" : "neutral"}>
            {t(`tier.${kb.data_policy.key_tier}`)}
          </Badge>
          <span className="font-mono text-xs text-ink-subtle">{kb.slug}</span>
        </div>
      </div>
    </div>
  );
}

// 지식 베이스 화면(설계 12.3 /knowledge/:id). 탭은 주소의 ?tab=이다.
export default function KnowledgeBasePage() {
  const { id = "" } = useParams();
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: TabKey = isTab(params.get("tab")) ? (params.get("tab") as TabKey) : "documents";
  const kb = useKnowledgeBase(id);
  const inProgress = kb.data?.indexing_document_count ?? 0;
  const stats = useKnowledgeStats(id, inProgress > 0);
  const defaults = useKnowledgeDefaults();
  const fields = useMetadataFields(id);
  const [uploading, setUploading] = useState(false);
  useRefreshWhenSettled(id, inProgress);

  if (kb.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (kb.isError) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <KbErrorAlert
          error={kb.error}
          title={t("detail.loadFailed")}
          onRetry={() => void kb.refetch()}
        />
      </div>
    );
  }
  const base = kb.data;
  const topKMax = defaults.data?.top_k_max ?? 20;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 py-6">
      <Header kb={base} />
      {stats.data ? <StatsBar stats={stats.data} /> : null}
      {stats.data && !stats.data.index_available ? (
        <IndexUnavailableNotice reason={stats.data.index_error} />
      ) : null}
      {stats.data?.vector_state === "stale" ? <Alert tone="info" title={t("stats.stale")} /> : null}
      {stats.data?.vector_state === "missing" && (stats.data.unit_count ?? 0) > 0 ? (
        <Alert tone="info" title={t("stats.missing")} />
      ) : null}
      {stats.data ? (
        <RateLimitNotice
          count={stats.data.daily_limited_documents}
          retryAfter={stats.data.daily_retry_after}
        />
      ) : null}
      <Tabs
        value={tab}
        onValueChange={(value) => {
          const next = new URLSearchParams(params);
          if (value === "documents") next.delete("tab");
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
        <TabsContent value="documents" className="pt-4">
          <DocumentTable
            kb={base}
            fields={fields.data?.items ?? []}
            onUpload={() => setUploading(true)}
            pollPaused={uploading}
          />
        </TabsContent>
        <TabsContent value="hitTest" className="pt-4">
          <HitTestPanel
            kb={base}
            topKMax={topKMax}
            queryLogDays={defaults.data?.query_log_days ?? 90}
          />
        </TabsContent>
        <TabsContent value="metadata" className="pt-4">
          <MetadataFieldsPanel kbId={base.id} />
        </TabsContent>
        <TabsContent value="eval" className="pt-4">
          <EvalPanel kb={base} topKMax={topKMax} />
        </TabsContent>
        <TabsContent value="settings" className="pt-4">
          <KbSettingsPanel key={base.id} kb={base} stats={stats.data} defaults={defaults.data} />
        </TabsContent>
        <TabsContent value="api" className="pt-4">
          <KbApiPanel kb={base} />
        </TabsContent>
      </Tabs>
      <UploadDialog
        key={base.id}
        kb={base}
        defaults={defaults.data}
        open={uploading}
        onOpenChange={setUploading}
      />
    </div>
  );
}
