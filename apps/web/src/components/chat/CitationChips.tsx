import { BookOpen, ExternalLink } from "lucide-react";
import { Popover } from "radix-ui";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import type { Citation } from "@/lib/citations";
import { cn } from "@/lib/cn";

function CitationPopover({ citation, chunkLinks }: { citation: Citation; chunkLinks: boolean }) {
  const { t } = useTranslation("chat");
  const label = t("citations.chip", { position: citation.position, name: citation.document_name });
  // 작은 조각이 있으면 맥락 확장 문서라 이 청크는 큰 조각이다.
  const sourceKey = citation.child_chunks?.length ? "citations.sourceParent" : "citations.source";
  return (
    <Popover.Root>
      <Popover.Trigger
        aria-label={label}
        title={citation.document_name}
        className={cn(
          "inline-flex h-6 max-w-56 items-center gap-1 rounded-sm border border-line bg-surface px-1.5 text-xs text-ink",
          "hover:border-line-strong hover:bg-hover focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
          "data-[state=open]:border-accent data-[state=open]:text-accent",
        )}
      >
        <span className="shrink-0 font-mono text-ink-muted">[{citation.position}]</span>
        <span className="min-w-0 truncate">{citation.document_name}</span>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          collisionPadding={8}
          aria-label={t("citations.detail", { position: citation.position })}
          className="z-50 flex max-h-[min(24rem,var(--radix-popover-content-available-height,24rem))] w-[min(26rem,calc(100vw-2rem))] flex-col gap-2 rounded-md border border-line bg-surface p-3 text-sm shadow-lg"
        >
          <header className="flex flex-col gap-0.5">
            <p className="font-medium break-words text-ink">
              <span className="mr-1 font-mono text-ink-muted">[{citation.position}]</span>
              {citation.document_name}
            </p>
            <p className="text-xs text-ink-muted">
              {t(sourceKey, { kb: citation.dataset_name, segment: citation.segment_position })}
              {" · "}
              <span className="tabular-nums">
                {t("citations.score", { score: citation.score.toFixed(2) })}
              </span>
            </p>
          </header>
          <p className="min-h-0 overflow-y-auto rounded-sm bg-sunken px-2.5 py-2 text-xs leading-relaxed whitespace-pre-wrap text-ink">
            {citation.content}
            {citation.answer ? (
              <>
                {"\n\n"}
                <span className="font-medium">{t("citations.answer")}: </span>
                {citation.answer}
              </>
            ) : null}
          </p>
          {citation.child_chunks?.length ? (
            <section className="flex flex-col gap-1">
              <h4 className="text-xs font-medium text-ink-muted">{t("citations.children")}</h4>
              <ul className="flex flex-col gap-1 border-l-2 border-accent/40 pl-2">
                {citation.child_chunks.map((child) => (
                  <li key={child.id} className="text-xs whitespace-pre-wrap text-ink">
                    <span className="mr-1.5 text-ink-muted">
                      {t("citations.child", { position: child.position })}
                    </span>
                    {child.content}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          {chunkLinks ? (
            // 저장본은 글을 2,000자까지만 갖고 있다. 전문은 청크 화면에서 본다.
            <Link
              to={`/knowledge/${citation.dataset_id}/documents/${citation.document_id}`}
              className="inline-flex items-center gap-1 self-start text-xs text-accent hover:underline"
            >
              <ExternalLink className="size-3.5" aria-hidden />
              {t("citations.fullText")}
            </Link>
          ) : null}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/*
 * 답 아래 인용 칩(3단계 설계 8.3, 12.3). position은 런 전체에서 세그먼트마다 하나이고 답의 [n]과 같아 그
 * 순서로 놓는다. 누르면 문서 이름·지식 베이스·점수·내용을 팝오버로 보이고, 콘솔에서는 청크 화면 링크를 둔다.
 */
export function CitationChips({
  citations,
  chunkLinks = false,
  className,
}: {
  citations: readonly Citation[];
  chunkLinks?: boolean;
  className?: string;
}) {
  const { t } = useTranslation("chat");
  if (citations.length === 0) return null;
  const ordered = [...citations].sort((a, b) => a.position - b.position);
  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      <span className="inline-flex items-center gap-1 text-xs text-ink-subtle">
        <BookOpen className="size-3.5" aria-hidden />
        {t("citations.label")}
      </span>
      <ul aria-label={t("citations.label")} className="flex flex-wrap gap-1.5">
        {ordered.map((citation) => (
          <li key={citation.segment_id}>
            <CitationPopover citation={citation} chunkLinks={chunkLinks} />
          </li>
        ))}
      </ul>
    </div>
  );
}
