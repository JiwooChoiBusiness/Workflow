import { FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";

export interface KnowledgeResultItem {
  position: number;
  score: number | null;
  datasetId: string;
  datasetName: string;
  documentId: string;
  documentName: string;
  segmentPosition: number | null;
  content: string;
  // 맥락 확장 문서에서 맞은 작은 조각 수
  children: number;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/*
 * knowledge_retrieval의 result 출력(설계 8.1: {content, title, metadata{score, dataset_*, document_*,
 * segment_position, position}})을 읽는다. 4KB 미리보기에서 잘린 항목이나 다른 모양은 건너뛴다.
 */
export function knowledgeResults(value: unknown): KnowledgeResultItem[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item, index) => {
    const entry = record(item);
    const metadata = record(entry?.metadata);
    if (!entry || !metadata) return [];
    return [
      {
        position: numberOrNull(metadata.position) ?? index + 1,
        score: numberOrNull(metadata.score),
        datasetId: text(metadata.dataset_id),
        datasetName: text(metadata.dataset_name),
        documentId: text(metadata.document_id),
        documentName: text(metadata.document_name) || text(entry.title),
        segmentPosition: numberOrNull(metadata.segment_position),
        content: text(entry.content),
        children: Array.isArray(metadata.child_chunks) ? metadata.child_chunks.length : 0,
      },
    ];
  });
}

// knowledge_retrieval 노드 카드의 결과 목록(설계 12.3): 순위·점수·문서·청크 위치와 내용 앞부분.
export function KnowledgeResultList({
  items,
  links = false,
}: {
  items: readonly KnowledgeResultItem[];
  // 문서 이름을 청크 화면으로 잇는다(콘솔만).
  links?: boolean;
}) {
  const { t } = useTranslation("workflows");
  if (items.length === 0) {
    return <p className="text-xs text-ink-subtle">{t("node.knowledge.none")}</p>;
  }
  return (
    <ol aria-label={t("node.knowledge.results")} className="flex flex-col gap-1.5">
      {items.map((item) => (
        <li
          key={`${item.position}:${item.documentId}:${item.segmentPosition ?? ""}`}
          className="flex flex-col gap-0.5 rounded-md border border-line px-2.5 py-1.5"
        >
          <div className="flex min-w-0 items-center gap-1.5 text-xs">
            <Badge tone="accent">#{item.position}</Badge>
            {item.score !== null ? (
              <span className="font-mono text-ink tabular-nums" title={t("node.knowledge.score")}>
                {item.score.toFixed(3)}
              </span>
            ) : null}
            <FileText className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
            {links && item.datasetId && item.documentId ? (
              <Link
                to={`/knowledge/${item.datasetId}/documents/${item.documentId}`}
                className="min-w-0 truncate text-accent hover:underline"
              >
                {item.documentName}
              </Link>
            ) : (
              <span className="min-w-0 truncate text-ink">{item.documentName}</span>
            )}
          </div>
          <p className="truncate text-xs text-ink-subtle">
            {item.segmentPosition !== null
              ? t(item.children > 0 ? "node.knowledge.whereParent" : "node.knowledge.where", {
                  kb: item.datasetName,
                  segment: item.segmentPosition,
                })
              : item.datasetName}
          </p>
          {item.content ? (
            <p className="line-clamp-2 text-xs whitespace-pre-wrap text-ink-muted">
              {item.content}
            </p>
          ) : null}
          {item.children > 0 ? (
            <p className="text-xs text-ink-subtle">
              {t("node.knowledge.children", { count: item.children })}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

// knowledge_index 노드 카드의 문서 링크(설계 12.3). 문서 id는 출력, 지식 베이스는 입력에 있다.
export function IndexedDocumentLinks({
  kbId,
  documentIds,
}: {
  kbId: string;
  documentIds: readonly string[];
}) {
  const { t } = useTranslation("workflows");
  if (!kbId || documentIds.length === 0) return null;
  return (
    <ul aria-label={t("node.knowledge.documents")} className="flex flex-wrap gap-1.5">
      {documentIds.map((id, index) => (
        <li key={id}>
          <Link
            to={`/knowledge/${kbId}/documents/${id}`}
            className="inline-flex items-center gap-1 rounded-sm border border-line px-1.5 py-0.5 text-xs text-accent hover:bg-hover"
          >
            <FileText className="size-3.5" aria-hidden />
            {t("node.knowledge.document", { n: index + 1 })}
          </Link>
        </li>
      ))}
    </ul>
  );
}
