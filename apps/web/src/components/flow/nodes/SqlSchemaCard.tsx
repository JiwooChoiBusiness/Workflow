import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { useSqlSources } from "@/features/sql/api";
import type { FlowNode } from "@/features/workflows/graph";

function names(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

// sql_schema 카드 요약: 데이터 소스 이름과 넣을 표(비우면 전부), 없는 소스·준비 필요·유료 키 표시.
export function SqlSchemaCard({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const sources = useSqlSources();
  const sourceId = typeof node.config.source_id === "string" ? node.config.source_id : "";
  if (!sourceId) return <p className="truncate">{t("card.sqlPickSource")}</p>;
  const source = sources.data?.find((item) => item.id === sourceId);
  const tables = names(node.config.tables);
  const missing = sources.isSuccess && !source;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <p className="truncate" title={tables.join(", ")}>
        {source?.name ?? t("card.sqlUnknownSource")} ·{" "}
        {tables.length ? t("card.sqlTables", { count: tables.length }) : t("card.sqlAllTables")}
      </p>
      <SqlSourceBadges
        missing={missing}
        notReady={source ? source.state !== "ready" : false}
        paid={source?.key_tier === "paid"}
      />
    </div>
  );
}

export function SqlSourceBadges({
  missing,
  notReady,
  paid,
  approval = false,
}: {
  missing: boolean;
  notReady: boolean;
  paid: boolean;
  approval?: boolean;
}) {
  const { t } = useTranslation("workflows");
  if (!missing && !notReady && !paid && !approval) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {missing ? <Badge tone="danger">{t("card.sqlMissing")}</Badge> : null}
      {notReady ? <Badge tone="warn">{t("card.sqlNotReady")}</Badge> : null}
      {approval ? <Badge tone="info">{t("card.sqlApproval")}</Badge> : null}
      {paid ? <Badge tone="warn">{t("forms.knowledge.paidBadge")}</Badge> : null}
    </div>
  );
}
