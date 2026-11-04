import { BookmarkPlus, ChevronDown } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type {
  IndexingTechnique,
  RetrievalSettings,
  RetrievalWarning,
  RetrieveRecord,
} from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { recordScoreKind } from "@/features/knowledge/retrievalSettings";
import { cn } from "@/lib/cn";

function score(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? "-" : value.toFixed(digits);
}

// 검색 시험 결과 카드(설계 7.7, 12.3). 점수가 코사인 원값인지, 결합 점수인지, bm25 기준인지 구분해 보이고
// 결합 내역(코사인 원값·벡터 min-max·키워드 bm25/최고·순위)을 함께 둔다.
export function ResultCard({
  kbId,
  record,
  position,
  settings,
  technique,
  warnings,
  barScale,
  onMark,
  marking,
}: {
  kbId: string;
  record: RetrieveRecord;
  position: number;
  settings: Pick<RetrievalSettings, "search_method" | "reranking_mode">;
  technique: IndexingTechnique;
  warnings: RetrievalWarning[];
  // 막대 길이의 기준. RRF처럼 값이 작은 점수는 1위 점수로 나눠 그린다.
  barScale: number;
  onMark?: (target: "segment" | "document") => void;
  marking?: boolean;
}) {
  const { t } = useTranslation("knowledge");
  const [open, setOpen] = useState(false);
  const kind = recordScoreKind(record, settings, technique, warnings);
  const segment = record.segment;
  const fusion = record.fusion;
  const width = Math.max(0, Math.min(1, barScale > 0 ? record.score / barScale : 0)) * 100;
  const long = segment.content.length > 400;
  return (
    <article
      aria-label={t("hitTest.position", { position })}
      className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3"
    >
      <header className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{t("hitTest.position", { position })}</Badge>
        <div className="flex min-w-40 items-center gap-2" title={t(`score.hints.${kind}`)}>
          <span className="font-mono text-sm font-semibold text-ink tabular-nums">
            {score(record.score)}
          </span>
          <span className="text-xs text-ink-muted">{t(`score.${kind}`)}</span>
          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-sunken" aria-hidden>
            <div className="h-full bg-accent" style={{ width: `${width}%` }} />
          </div>
        </div>
        <Link
          to={`/knowledge/${kbId}/documents/${segment.document_id}`}
          className="min-w-0 truncate text-sm text-ink hover:underline"
        >
          {segment.document.name}
        </Link>
        <span className="text-xs text-ink-subtle">
          {t("hitTest.segment", { position: segment.position })}
        </span>
        {segment.document.indexing_status !== "completed" ? (
          <Badge tone="info">{t("hitTest.notCompleted")}</Badge>
        ) : null}
        {onMark ? (
          <Menu>
            <MenuTrigger asChild>
              <Button size="sm" variant="ghost" className="ml-auto" loading={marking}>
                <BookmarkPlus className="size-3.5" aria-hidden />
                {t("hitTest.markExpected")}
              </Button>
            </MenuTrigger>
            <MenuContent align="end">
              <MenuItem onSelect={() => onMark("segment")}>{t("hitTest.markSegment")}</MenuItem>
              <MenuItem onSelect={() => onMark("document")}>{t("hitTest.markDocument")}</MenuItem>
            </MenuContent>
          </Menu>
        ) : null}
      </header>
      <p className={cn("text-sm whitespace-pre-wrap text-ink", !open && long && "line-clamp-6")}>
        {segment.content}
      </p>
      {long ? (
        <Button
          size="sm"
          variant="ghost"
          className="self-start"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <ChevronDown
            className={cn("size-3.5 transition-transform", open && "rotate-180")}
            aria-hidden
          />
          {open ? t("hitTest.collapse") : t("hitTest.expand")}
        </Button>
      ) : null}
      {segment.answer ? (
        <p className="text-sm whitespace-pre-wrap text-ink-muted">
          <span className="font-medium text-ink">{t("hitTest.answer")}: </span>
          {segment.answer}
        </p>
      ) : null}
      {record.child_chunks && record.child_chunks.length > 0 ? (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-ink-muted">{t("hitTest.children")}</p>
          <ul className="flex flex-col gap-1 border-l-2 border-accent/40 pl-2">
            {record.child_chunks.map((child) => (
              <li key={child.id} className="text-xs text-ink">
                <span className="mr-1.5 font-mono text-ink-muted">{score(child.score)}</span>
                <span className="whitespace-pre-wrap">{child.content}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {fusion ? (
        <dl
          aria-label={t("score.breakdown")}
          className="flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-2 text-xs text-ink-muted"
        >
          <div className="flex gap-1">
            <dt>{t("score.rawCosine")}</dt>
            <dd className="font-mono text-ink">{score(fusion.cosine)}</dd>
          </div>
          <div className="flex gap-1">
            <dt>{t("score.vectorScore")}</dt>
            <dd className="font-mono text-ink">{score(fusion.vector_score)}</dd>
          </div>
          <div className="flex gap-1">
            <dt>{t("score.keywordScore")}</dt>
            <dd className="font-mono text-ink">{score(fusion.keyword_score)}</dd>
          </div>
          <div>
            {t("score.ranks", {
              vector: fusion.vector_rank ?? "-",
              keyword: fusion.keyword_rank ?? "-",
            })}
          </div>
          {record.fusion_score !== null && record.fusion_score !== undefined ? (
            <div>{t("score.fusionScore", { value: score(record.fusion_score) })}</div>
          ) : null}
        </dl>
      ) : null}
    </article>
  );
}
