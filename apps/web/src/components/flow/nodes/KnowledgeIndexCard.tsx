import { useTranslation } from "react-i18next";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { useKnowledgeBases } from "@/features/knowledge/api";
import { useKbOptions } from "@/features/workflows/api";
import type { FlowNode } from "@/features/workflows/graph";
import { useOptionalWfRun } from "@/features/workflows/wfRunContext";

const STATUS_TONE: Record<string, BadgeTone> = {
  queued: "info",
  skipped: "neutral",
  completed: "ok",
  error: "danger",
  paused: "warn",
};

type IndexStatus = "queued" | "skipped" | "completed" | "error" | "paused";

/*
 * knowledge_index 카드 요약: 지식 베이스 이름과 문서 수, 넣는 것(글·파일), 마지막 실행의 출력 status
 * (kb_options.index_statuses: wait를 끄면 queued·skipped, 켜면 색인 결과까지).
 */
export function KnowledgeIndexCard({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const kbs = useKnowledgeBases();
  const statuses = useKbOptions().data?.index_statuses;
  const status = useOptionalWfRun((state) => {
    const id = state.latestByNode[node.id];
    const preview = id ? state.nodeRuns[id]?.outputsPreview : null;
    return typeof preview?.status === "string" ? preview.status : null;
  });
  const kbId = typeof node.config.kb_id === "string" ? node.config.kb_id : "";
  if (!kbId) return <p className="truncate">{t("card.pickKb")}</p>;
  const kb = kbs.data?.find((item) => item.id === kbId);
  const source = (node.config.source ?? {}) as { files_var?: unknown };
  const kindLabel = t(source.files_var ? "card.indexFiles" : "card.indexText");
  const known = status !== null && (statuses ?? Object.keys(STATUS_TONE)).includes(status);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <p className="truncate">
        {kb ? t("card.indexKb", { name: kb.name, count: kb.document_count }) : t("card.unknownKb")}{" "}
        · {kindLabel}
      </p>
      {known || (kbs.isSuccess && !kb) ? (
        <div className="flex flex-wrap gap-1">
          {kbs.isSuccess && !kb ? <Badge tone="danger">{t("card.kbMissing")}</Badge> : null}
          {known ? (
            <Badge tone={STATUS_TONE[status] ?? "neutral"}>
              {t(`card.indexStatus.${status as IndexStatus}`)}
            </Badge>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
