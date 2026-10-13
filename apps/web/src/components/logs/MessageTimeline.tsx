import {
  Download,
  FoldVertical,
  Image as ImageIcon,
  MessagesSquare,
  Paperclip,
} from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { AssistantMessage } from "@/components/chat/AssistantMessage";
import { ToolIcon } from "@/components/chat/ToolIcon";
import { summarizeInput } from "@/components/chat/toolSummary";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { sessionFileUrl, useSessionMessages } from "@/features/logs/api";
import type { MessageOut } from "@/features/logs/types";
import { cn } from "@/lib/cn";
import { formatBytes, formatDateTime } from "@/lib/format";

type Block = MessageOut["blocks"][number];

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 실행 정보(<context>) 블록은 모델에만 보내는 내용이라 화면에서 숨긴다(12.2 M6 이후 확정).
function visibleBlocks(message: MessageOut): Block[] {
  return message.blocks.filter((block) => block.kind !== "context");
}

const NORMAL_STOPS = new Set(["end_turn", "tool_use"]);

function Files({ sessionId, message }: { sessionId: string; message: MessageOut }) {
  const { t } = useTranslation("logs");
  if (message.files.length === 0) return null;
  return (
    <ul className="mt-1.5 flex flex-wrap gap-1.5">
      {message.files.map((file) => {
        const name = file.filename ?? file.path;
        return (
          <li key={file.session_file_id ?? file.path}>
            {file.downloadable && file.session_file_id ? (
              <a
                href={sessionFileUrl(sessionId, file.session_file_id)}
                download
                className="inline-flex items-center gap-1 rounded-sm border border-line bg-surface px-1.5 py-0.5 text-xs text-accent hover:bg-hover"
                title={t("timeline.download", { name })}
              >
                <Download className="size-3" aria-hidden />
                <span className="font-mono">{file.path}</span>
                <span className="text-ink-subtle">{formatBytes(file.size)}</span>
              </a>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-sm border border-line px-1.5 py-0.5 text-xs text-ink-muted">
                <Paperclip className="size-3" aria-hidden />
                <span className="font-mono">{file.path}</span>
                <span>{t("timeline.notStored")}</span>
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ToolUse({ block, onOpen }: { block: Block; onOpen?: (toolCallId: string) => void }) {
  const { t } = useTranslation("logs");
  const name = text(block.name);
  const input = isRecord(block.input) ? block.input : null;
  const id = text(block.id);
  return (
    <div className="flex items-center gap-2 rounded-md border border-line bg-surface px-2.5 py-1.5">
      <ToolIcon name={name} className="size-4 shrink-0 text-ink-muted" />
      <span className="shrink-0 font-mono text-xs font-medium text-ink">{name}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">
        {summarizeInput(name, input, "")}
      </span>
      {onOpen && id ? (
        <Button size="sm" variant="ghost" onClick={() => onOpen(id)}>
          {t("timeline.inspect")}
        </Button>
      ) : null}
    </div>
  );
}

const encoder = new TextEncoder();

function ToolResult({ block }: { block: Block }) {
  const { t } = useTranslation("logs");
  const content = text(block.content);
  return (
    <details className="group rounded-md border border-line/70 text-xs">
      <summary className="flex cursor-pointer items-center gap-2 px-2.5 py-1 text-ink-muted select-none hover:text-ink">
        {t("timeline.toolResult")}
        {block.is_error ? <Badge tone="danger">{t("timeline.toolError")}</Badge> : null}
        <span className="text-ink-subtle">{formatBytes(encoder.encode(content).length)}</span>
      </summary>
      <pre className="max-h-60 overflow-auto border-t border-line/70 bg-code px-2.5 py-2 font-mono whitespace-pre-wrap text-code-ink">
        {content || t("timeline.empty")}
      </pre>
    </details>
  );
}

function Meta({
  message,
  onSelectRun,
  selected,
}: {
  message: MessageOut;
  onSelectRun?: (runId: string) => void;
  selected: boolean;
}) {
  const { t } = useTranslation("logs");
  return (
    <div className="flex items-center gap-2 text-xs text-ink-subtle">
      <time dateTime={message.created_at}>{formatDateTime(message.created_at)}</time>
      {message.run_id && onSelectRun ? (
        <button
          type="button"
          onClick={() => onSelectRun(message.run_id!)}
          className={cn(
            "rounded-sm px-1 hover:bg-hover hover:text-ink",
            selected && "font-medium text-accent",
          )}
        >
          {selected ? t("timeline.runSelected") : t("timeline.selectRun")}
        </button>
      ) : null}
    </div>
  );
}

function MessageEntry({
  sessionId,
  message,
  selectedRunId,
  onSelectRun,
  onOpenTool,
}: {
  sessionId: string;
  message: MessageOut;
  selectedRunId: string | null;
  onSelectRun?: (runId: string) => void;
  onOpenTool?: (runId: string, toolCallId: string) => void;
}) {
  const { t } = useTranslation("logs");
  const selected = message.run_id !== null && message.run_id === selectedRunId;
  if (message.role === "system_summary") {
    return (
      <details className="text-xs text-ink-subtle">
        <summary className="flex cursor-pointer items-center gap-3 py-1 select-none">
          <span className="h-px flex-1 bg-line" />
          <FoldVertical className="size-3.5" aria-hidden />
          {t("timeline.summary")}
          <span className="h-px flex-1 bg-line" />
        </summary>
        <p className="mt-1 rounded-md bg-sunken px-3 py-2 whitespace-pre-wrap text-ink-muted">
          {message.text}
        </p>
      </details>
    );
  }
  if (message.role === "user") {
    const images = message.blocks.filter((block) => block.type === "image");
    return (
      <div className="flex flex-col items-end gap-1">
        <div
          className={cn(
            "max-w-[90%] rounded-lg bg-accent-soft px-3 py-2 text-sm whitespace-pre-wrap text-ink",
            selected && "ring-2 ring-accent/40",
          )}
        >
          <span className="sr-only">{t("timeline.user")}: </span>
          {message.text}
          {images.length > 0 ? (
            <span className="mt-1 flex flex-wrap gap-1.5 text-xs text-ink-muted">
              {images.map((image, index) => (
                <span key={index} className="inline-flex items-center gap-1">
                  <ImageIcon className="size-3" aria-hidden />
                  {text(image.filename) || t("timeline.image")}
                </span>
              ))}
            </span>
          ) : null}
        </div>
        <Files sessionId={sessionId} message={message} />
        <Meta message={message} onSelectRun={onSelectRun} selected={selected} />
      </div>
    );
  }
  const parts: ReactNode[] = visibleBlocks(message).map((block, index) => {
    if (block.type === "text") {
      const value = text(block.text);
      return value ? <AssistantMessage key={index} text={value} streaming={false} /> : null;
    }
    if (block.type === "tool_use") {
      return (
        <ToolUse
          key={index}
          block={block}
          onOpen={
            message.run_id && onOpenTool
              ? (toolCallId) => onOpenTool(message.run_id!, toolCallId)
              : undefined
          }
        />
      );
    }
    if (block.type === "tool_result") return <ToolResult key={index} block={block} />;
    return null;
  });
  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-md border-l-2 pl-3",
        selected ? "border-accent" : "border-transparent",
      )}
    >
      {parts}
      {message.stop_reason && !NORMAL_STOPS.has(message.stop_reason) ? (
        <Badge
          tone={message.stop_reason === "cancelled" ? "neutral" : "warn"}
          className="self-start"
        >
          {t("timeline.stopReason", { reason: message.stop_reason })}
        </Badge>
      ) : null}
      <Files sessionId={sessionId} message={message} />
      <Meta message={message} onSelectRun={onSelectRun} selected={selected} />
    </div>
  );
}

export function MessageTimeline({
  sessionId,
  selectedRunId,
  onSelectRun,
  onOpenTool,
}: {
  sessionId: string;
  selectedRunId: string | null;
  onSelectRun?: (runId: string) => void;
  onOpenTool?: (runId: string, toolCallId: string) => void;
}) {
  const { t } = useTranslation("logs");
  const query = useSessionMessages(sessionId);
  if (query.isPending) {
    return (
      <div className="flex justify-center p-6">
        <Spinner label={t("timeline.loading")} />
      </div>
    );
  }
  if (query.isError) {
    return (
      <ErrorAlert
        error={query.error}
        title={t("timeline.loadFailed")}
        onRetry={() => void query.refetch()}
      />
    );
  }
  // 페이지는 최근 것부터 쌓이므로 거꾸로 이어 붙여 오래된 메시지가 위에 오게 한다.
  const messages = [...query.data.pages].reverse().flatMap((page) => page.items);
  if (messages.length === 0) {
    return <EmptyState icon={MessagesSquare} title={t("timeline.none")} />;
  }
  return (
    <div className="flex flex-col gap-4">
      {query.hasNextPage ? (
        <Button
          size="sm"
          variant="ghost"
          className="self-center"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {t("timeline.earlier")}
        </Button>
      ) : null}
      {messages.map((message) => (
        <MessageEntry
          key={message.id}
          sessionId={sessionId}
          message={message}
          selectedRunId={selectedRunId}
          onSelectRun={onSelectRun}
          onOpenTool={onOpenTool}
        />
      ))}
    </div>
  );
}
