import { ChevronRight, Download } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/cn";
import { formatBytes, formatDuration, formatNumber } from "@/lib/format";
import type { ToolCallView } from "@/stores/runStore";
import { useRunStore } from "@/stores/runStoreContext";
import { ToolIcon } from "./ToolIcon";
import { prettyJson, summarizeInput } from "./toolSummary";

const RESULT_TONE: Record<string, BadgeTone> = {
  ok: "ok",
  error: "danger",
  timeout: "warn",
  cancelled: "neutral",
  rejected: "neutral",
};

function StatusBadge({ tool }: { tool: ToolCallView }) {
  const { t } = useTranslation("chat");
  if (tool.phase === "done" && tool.result) {
    return (
      <Badge tone={RESULT_TONE[tool.result] ?? "neutral"}>{t(`tool.result.${tool.result}`)}</Badge>
    );
  }
  if (tool.phase === "done") return null;
  const tone: BadgeTone = tool.phase === "awaiting_approval" ? "warn" : "info";
  return (
    <Badge tone={tone}>
      {tool.phase === "running" || tool.phase === "input" ? <Spinner className="size-3" /> : null}
      {t(`tool.phase.${tool.phase}`)}
    </Badge>
  );
}

function ToolOutput({ tool }: { tool: ToolCallView }) {
  const { t } = useTranslation("chat");
  const ref = useRef<HTMLPreElement>(null);
  const stickRef = useRef(true);

  // 사용자가 위로 올려 읽는 중이 아니면 새 출력이 올 때 맨 아래를 보여 준다.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [tool.outputChars, tool.droppedChars]);

  if (tool.output.length === 0) {
    return <p className="text-xs text-ink-subtle">{t("tool.noOutput")}</p>;
  }
  return (
    <pre
      ref={ref}
      onScroll={(event) => {
        const el = event.currentTarget;
        stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
      className="max-h-80 overflow-auto rounded-md bg-term px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-term-ink"
    >
      {tool.output.map((chunk, index) => (
        <span key={index} className={chunk.stream === "stderr" ? "text-term-err" : undefined}>
          {chunk.text}
        </span>
      ))}
    </pre>
  );
}

export function ToolCallCard({ toolCallId }: { toolCallId: string }) {
  const { t } = useTranslation("chat");
  const tool = useRunStore((state) => state.toolCalls[toolCallId]);
  const [open, setOpen] = useState(false);
  if (!tool) return null;

  const summary = summarizeInput(tool.name, tool.input, tool.inputText);
  const done = tool.phase === "done";
  const restoredResult = tool.restored && tool.result !== "cancelled";
  const showDownload =
    done &&
    (tool.droppedChars > 0 ||
      tool.outputIsExcerpt ||
      Boolean(tool.outputStorageKey) ||
      restoredResult);
  const outputUrl = `/api/console/runs/${encodeURIComponent(tool.runId)}/tool-calls/${encodeURIComponent(tool.id)}/output`;

  return (
    <Collapsible.Root
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "rounded-md border bg-surface",
        tool.phase === "awaiting_approval" ? "border-warn/60" : "border-line",
      )}
    >
      <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-hover">
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 text-ink-subtle transition-transform",
            open && "rotate-90",
          )}
          aria-hidden
        />
        <ToolIcon name={tool.name} className="size-4 shrink-0 text-ink-muted" />
        <span className="shrink-0 font-mono text-xs font-medium text-ink">{tool.name || "…"}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">{summary}</span>
        <StatusBadge tool={tool} />
        {tool.durationMs !== null ? (
          <span className="shrink-0 text-xs text-ink-subtle tabular-nums">
            {formatDuration(tool.durationMs)}
          </span>
        ) : null}
      </Collapsible.Trigger>
      <Collapsible.Content className="flex flex-col gap-3 border-t border-line px-3 py-3">
        <section>
          <h4 className="mb-1 text-xs font-medium text-ink-muted">{t("tool.input")}</h4>
          <pre className="max-h-64 overflow-auto rounded-md bg-code px-3 py-2 font-mono text-xs whitespace-pre-wrap text-code-ink">
            {tool.input ? prettyJson(tool.input) : tool.inputText || "…"}
          </pre>
        </section>
        <section>
          <h4 className="mb-1 text-xs font-medium text-ink-muted">{t("tool.output")}</h4>
          {tool.droppedChars > 0 ? (
            <p className="mb-1 text-xs text-ink-muted">
              {t("tool.dropped", { count: formatNumber(tool.droppedChars) })}
            </p>
          ) : null}
          {tool.outputIsExcerpt ? (
            <p className="mb-1 text-xs text-ink-muted">{t("tool.excerpt")}</p>
          ) : null}
          {restoredResult ? (
            <p className="mb-1 text-xs text-ink-muted">{t("tool.restored")}</p>
          ) : null}
          <ToolOutput tool={tool} />
        </section>
        {done ? (
          <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-muted">
            {tool.exitCode !== null ? (
              <span className={tool.exitCode === 0 ? undefined : "text-danger"}>
                {t("tool.exitCode", { code: tool.exitCode })}
              </span>
            ) : null}
            {tool.durationMs !== null ? <span>{formatDuration(tool.durationMs)}</span> : null}
            {tool.peakMemoryBytes ? (
              <span>{t("tool.peakMemory", { size: formatBytes(tool.peakMemoryBytes) })}</span>
            ) : null}
            {tool.outputBytes !== null ? (
              <span>{t("tool.outputSize", { size: formatBytes(tool.outputBytes) })}</span>
            ) : null}
            {showDownload ? (
              <a
                href={outputUrl}
                className="inline-flex items-center gap-1 text-accent hover:underline"
                download
              >
                <Download className="size-3.5" aria-hidden />
                {t("tool.fullOutput")}
              </a>
            ) : null}
          </footer>
        ) : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
