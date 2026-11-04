import { useTranslation } from "react-i18next";
import { useSqlSources } from "@/features/sql/api";
import type { FlowNode } from "@/features/workflows/graph";
import { SqlSourceBadges } from "./SqlSchemaCard";

function firstLine(value: unknown): string {
  return typeof value === "string" ? (value.trim().split(/\r?\n/)[0] ?? "") : "";
}

/*
 * sql_query 카드 요약: 데이터 소스 이름과 SQL 첫 줄(또는 SQL 변수). 실행 전 승인은 노드 값, 비웠으면 데이터
 * 소스의 승인 설정으로 표시한다.
 */
export function SqlQueryCard({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const sources = useSqlSources();
  const config = node.config;
  const sourceId = typeof config.source_id === "string" ? config.source_id : "";
  if (!sourceId) return <p className="truncate">{t("card.sqlPickSource")}</p>;
  const source = sources.data?.find((item) => item.id === sourceId);
  const sqlVar = typeof config.sql_var === "string" ? config.sql_var : "";
  const sql = firstLine(config.sql);
  const approval =
    config.approval === "always" || (config.approval == null && source?.approval === "always");
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <p className="truncate">{source?.name ?? t("card.sqlUnknownSource")}</p>
      <p className="truncate font-mono" title={sqlVar || sql}>
        {sqlVar ? `${t("card.sqlVar")} ${sqlVar}` : sql || t("card.sqlNoSql")}
      </p>
      <SqlSourceBadges
        missing={sources.isSuccess && !source}
        notReady={source ? source.state !== "ready" : false}
        paid={source?.key_tier === "paid"}
        approval={approval}
      />
    </div>
  );
}
