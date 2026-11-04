import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useKnowledgeBases } from "@/features/knowledge/api";
import type { FlowNode } from "@/features/workflows/graph";

function ids(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

// knowledge_retrieval 카드 요약: 고른 지식 베이스 수와 이름, 필터·유료 키·색인 중 표시.
export function KnowledgeRetrievalCard({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const kbs = useKnowledgeBases();
  const kbIds = ids(node.config.kb_ids);
  if (kbIds.length === 0) return <p className="truncate">{t("card.pickKb")}</p>;
  const byId = new Map((kbs.data ?? []).map((kb) => [kb.id, kb]));
  const found = kbIds.flatMap((id) => {
    const kb = byId.get(id);
    return kb ? [kb] : [];
  });
  const names = kbIds.map((id) => byId.get(id)?.name ?? t("card.unknownKb"));
  const filter = (node.config.metadata_filter ?? {}) as { mode?: unknown };
  const missing = kbs.isSuccess && found.length < kbIds.length;
  const indexing = found.some((kb) => kb.indexing_document_count > 0);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <p className="truncate" title={names.join(", ")}>
        {t("card.kbs", { count: kbIds.length })} · {names.join(", ")}
      </p>
      {missing || indexing || filter.mode === "manual" || filter.mode === "automatic" ? (
        <div className="flex flex-wrap gap-1">
          {missing ? <Badge tone="danger">{t("card.kbMissing")}</Badge> : null}
          {indexing ? <Badge tone="info">{t("card.kbIndexing")}</Badge> : null}
          {filter.mode === "manual" || filter.mode === "automatic" ? (
            <Badge>{t(`card.filter.${filter.mode}`)}</Badge>
          ) : null}
        </div>
      ) : null}
      {found.some((kb) => kb.data_policy.key_tier === "paid") ? (
        <Badge tone="warn" className="self-start">
          {t("forms.knowledge.paidBadge")}
        </Badge>
      ) : null}
    </div>
  );
}
