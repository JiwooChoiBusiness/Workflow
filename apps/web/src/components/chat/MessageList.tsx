import { Check, CircleAlert, Info, MessageSquareReply, Paperclip, X } from "lucide-react";
import { memo, useEffect, useMemo, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import type { Citation } from "@/lib/citations";
import { cn } from "@/lib/cn";
import { formatBytes, formatDateTime } from "@/lib/format";
import { RECONNECT_MAX_RETRIES } from "@/lib/sse";
import type { AttachmentView, PendingInteraction, TimelineItem } from "@/stores/runStore";
import { useRunStore } from "@/stores/runStoreContext";
import { AssistantMessage } from "./AssistantMessage";
import { CompactionDivider } from "./SessionBits";
import { ToolCallCard } from "./ToolCallCard";

type ItemOf<K extends TimelineItem["kind"]> = Extract<TimelineItem, { kind: K }>;

function NoticeLine({ item }: { item: ItemOf<"notice"> }) {
  const { t } = useTranslation("chat");
  let text: string;
  switch (item.code) {
    case "workspace_restored":
      text = item.count
        ? t("notice.workspace_restored_packages", { count: item.count })
        : t("notice.workspace_restored");
      break;
    case "workspace_restore_warning":
      text = t("notice.workspace_restore_warning", { message: item.message ?? "" });
      break;
    case "retrying":
    case "step_limit":
      text = t(`notice.${item.code}`, { count: item.count ?? 0 });
      break;
    default:
      text = t(`notice.${item.code}`);
  }
  const warn = item.code === "workspace_restore_warning" || item.code === "step_limit";
  return (
    <p className={`flex items-center gap-1.5 text-xs ${warn ? "text-warn" : "text-ink-subtle"}`}>
      <Info className="size-3.5 shrink-0" aria-hidden />
      {text}
    </p>
  );
}

function InteractionResult({ item }: { item: ItemOf<"interaction"> }) {
  const { t } = useTranslation("chat");
  let icon = <MessageSquareReply className="size-3.5" aria-hidden />;
  let text: string;
  if (item.decision === "approve") {
    icon = <Check className="size-3.5 text-ok" aria-hidden />;
    text = item.autoApprove ? t("interaction.approvedAuto") : t("interaction.approved");
  } else if (item.decision === "reject") {
    icon = <X className="size-3.5 text-danger" aria-hidden />;
    text = item.note
      ? t("interaction.rejectedNote", { note: item.note })
      : t("interaction.rejected");
  } else {
    text = t("interaction.answered", { answer: item.answer ?? "" });
  }
  return (
    <p className="flex items-start gap-1.5 text-xs text-ink-muted">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <span className="whitespace-pre-wrap">{text}</span>
    </p>
  );
}

export type FileHref = (sessionFileId: string) => string | null;

function Attachments({ files, fileHref }: { files: AttachmentView[]; fileHref?: FileHref }) {
  const { t } = useTranslation("chat");
  return (
    <ul aria-label={t("attachments.label")} className="mt-1.5 flex flex-col gap-1">
      {files.map((file, index) => {
        const href =
          fileHref && file.downloadable && file.sessionFileId ? fileHref(file.sessionFileId) : null;
        return (
          <li key={`${file.name}:${index}`} className="flex items-center gap-1.5 text-xs">
            <Paperclip className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
            {href ? (
              <a
                href={href}
                download
                className="min-w-0 truncate text-accent hover:underline"
                aria-label={t("attachments.download", { name: file.name })}
              >
                {file.name}
              </a>
            ) : (
              <span className="min-w-0 truncate">{file.name}</span>
            )}
            <span className="shrink-0 text-ink-subtle tabular-nums">{formatBytes(file.size)}</span>
          </li>
        );
      })}
    </ul>
  );
}

const TimelineEntry = memo(function TimelineEntry({
  item,
  fileHref,
  citations,
  citationLinks,
}: {
  item: TimelineItem;
  fileHref?: FileHref;
  citations?: readonly Citation[];
  citationLinks?: boolean;
}) {
  const { t } = useTranslation("chat");
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-lg bg-accent-soft px-3 py-2 text-sm text-ink">
            <p className="whitespace-pre-wrap">
              <span className="sr-only">{t("role.user")}: </span>
              {item.text}
            </p>
            {item.files?.length ? <Attachments files={item.files} fileHref={fileHref} /> : null}
          </div>
        </div>
      );
    case "text":
      return (
        <AssistantMessage
          text={item.text}
          streaming={!item.done}
          citations={citations}
          citationLinks={citationLinks}
        />
      );
    case "thinking":
      return (
        <details className="group text-xs text-ink-muted">
          <summary className="cursor-pointer select-none hover:text-ink">{t("thinking")}</summary>
          <p className="mt-1 border-l-2 border-line pl-3 whitespace-pre-wrap">{item.text}</p>
        </details>
      );
    case "tool":
      return (
        <ToolCallCard
          toolCallId={item.toolCallId}
          citationLinks={citationLinks}
          fileHref={fileHref}
        />
      );
    case "compaction":
      return <CompactionDivider before={item.beforeTokens} after={item.afterTokens} />;
    case "interaction":
      return <InteractionResult item={item} />;
    case "notice":
      return <NoticeLine item={item} />;
    case "error":
      return (
        <Alert tone="danger" title={t("error.title")}>
          <span className="block font-mono text-xs">{item.code}</span>
          <span className="block">{item.message}</span>
          {item.retryAt ? (
            <span className="block text-xs">
              {t("error.retryAt", { time: formatDateTime(item.retryAt) })}
            </span>
          ) : item.retryable ? (
            <span className="block text-xs">{t("error.retryable")}</span>
          ) : null}
        </Alert>
      );
  }
});

// 런마다 인용 칩을 붙일 항목: 그 런의 마지막 답 글(항목 id → 런 id).
export function citationAnchors(
  items: readonly TimelineItem[],
  citations: Readonly<Record<string, readonly Citation[]>>,
): Map<string, string> {
  const anchors = new Map<string, string>();
  const last = new Map<string, string>();
  for (const item of items) {
    if (item.kind === "text" && item.runId && citations[item.runId]?.length) {
      last.set(item.runId, item.id);
    }
  }
  for (const [runId, itemId] of last) anchors.set(itemId, runId);
  return anchors;
}

export function MessageList({
  renderInteraction,
  fileHref,
  footer,
  citationLinks = false,
  hideCitations = false,
  className,
}: {
  renderInteraction: (interaction: PendingInteraction) => ReactNode;
  // 첨부 파일 내려받기 주소. 콘솔과 웹앱의 경로가 다르다.
  fileHref?: FileHref;
  // 목록 끝에 둘 카드(챗플로우 human_input 폼처럼 승인·질문이 아닌 대기)
  footer?: ReactNode;
  // 인용 팝오버에 청크 화면 링크를 둔다(콘솔만).
  citationLinks?: boolean;
  // 웹앱 설정(citations_enabled)으로 인용 칩을 감춘다. API 응답에는 그대로 온다.
  hideCitations?: boolean;
  className?: string;
}) {
  const { t } = useTranslation("chat");
  const items = useRunStore((state) => state.items);
  const pending = useRunStore((state) => state.pendingInteraction);
  const citations = useRunStore((state) => state.citations);
  const anchors = useMemo(
    () => (hideCitations ? new Map<string, string>() : citationAnchors(items, citations)),
    [items, citations, hideCitations],
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // 내용 높이가 바뀔 때(델타, 도구 카드 펼침) 사용자가 맨 아래를 보고 있었으면 따라 내려간다.
  useEffect(() => {
    const scroller = scrollRef.current;
    const content = contentRef.current;
    if (!scroller || !content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (stickRef.current) scroller.scrollTop = scroller.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  const approvalToolId = pending?.kind === "approval" ? (pending.tool_call_id ?? null) : null;
  const approvalInline =
    approvalToolId !== null &&
    items.some((item) => item.kind === "tool" && item.toolCallId === approvalToolId);

  return (
    <div
      ref={scrollRef}
      // relative가 없으면 메시지 안의 absolute 요소(sr-only 라벨 등)가 이 스크롤 영역에 잡히지 않고
      // 바깥 컨테이너의 스크롤 높이를 늘려 채팅을 넘기면 페이지가 밀렸다.
      className={cn("relative min-h-0 flex-1 overflow-y-auto", className)}
      onScroll={(event) => {
        const el = event.currentTarget;
        stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      }}
    >
      <div ref={contentRef} className="flex flex-col gap-3 px-4 py-4">
        {items.length === 0 && !pending && !footer ? (
          <p className="py-10 text-center text-sm text-ink-subtle">{t("list.empty")}</p>
        ) : null}
        {items.map((item) => {
          const citedRun = anchors.get(item.id);
          return (
            <div key={item.id} className="flex flex-col gap-2">
              <TimelineEntry
                item={item}
                fileHref={fileHref}
                citations={citedRun ? citations[citedRun] : undefined}
                citationLinks={citationLinks}
              />
              {/* 승인 카드는 해당 도구 카드 바로 아래에 둔다(설계 3.10). */}
              {pending &&
              approvalInline &&
              item.kind === "tool" &&
              item.toolCallId === approvalToolId
                ? renderInteraction(pending)
                : null}
            </div>
          );
        })}
        {pending && !approvalInline ? renderInteraction(pending) : null}
        {footer}
      </div>
    </div>
  );
}

export function ConnectionNotice({ status, attempt }: { status: string; attempt?: number }) {
  const { t } = useTranslation("chat");
  if (status === "reconnecting") {
    return (
      <Alert
        tone="warn"
        className="mx-4 mt-3"
        title={t("connection.reconnecting", { attempt: attempt ?? 1, max: RECONNECT_MAX_RETRIES })}
      />
    );
  }
  if (status === "failed") {
    return (
      <div className="mx-4 mt-3 flex items-center gap-2 text-sm text-danger">
        <CircleAlert className="size-4" aria-hidden />
        {t("connection.failed")}
      </div>
    );
  }
  return null;
}
