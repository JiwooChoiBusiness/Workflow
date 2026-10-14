import { CircleAlert, CircleCheck, RefreshCw, TriangleAlert } from "lucide-react";
import { Popover } from "radix-ui";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { WfIssue } from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { nodeById } from "@/features/workflows/graph";
import type { Autosave, SaveStatus as Status } from "@/features/workflows/useDraftAutosave";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";

const STATUS_TONE: Record<Status, BadgeTone> = {
  saved: "ok",
  dirty: "neutral",
  saving: "info",
  failed: "danger",
  conflict: "warn",
};

// 상단 바의 저장 상태. 누르면 바로 저장한다(Ctrl+S와 같다).
export function SaveStatus({ autosave }: { autosave: Autosave }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const { status } = autosave;
  const title =
    status === "failed"
      ? t("editor.saveFailed", { message: errorMessage(autosave.error, tc) })
      : t("editor.saveNow");
  return (
    <button
      type="button"
      className="rounded-sm focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
      onClick={() => void autosave.flush()}
      title={title}
      aria-label={`${t("editor.statusLabel")}: ${t(`editor.status.${status}`)}. ${title}`}
    >
      <Badge tone={STATUS_TONE[status]}>
        {status === "saving" ? <Spinner className="size-3" /> : null}
        {t(`editor.status.${status}`)}
      </Badge>
    </button>
  );
}

function issueKey(issue: WfIssue, index: number): string {
  return `${issue.code}:${issue.node_id ?? ""}:${issue.edge_id ?? ""}:${issue.field ?? ""}:${index}`;
}

// 검증 결과 요약과 목록(설계 7.2 상단 바). 항목을 누르면 그 노드를 고른다.
export function IssuesPopover({
  onSelectNode,
  onRevalidate,
}: {
  onSelectNode: (nodeId: string) => void;
  onRevalidate: () => Promise<void>;
}) {
  const { t } = useTranslation("workflows");
  const issues = useFlow((state) => state.issues);
  const graph = useFlow((state) => state.graph);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const errors = issues.filter((issue) => issue.level === "error").length;
  const warnings = issues.length - errors;

  let summary;
  if (errors > 0) {
    summary = (
      <>
        <CircleAlert className="size-4 text-danger" aria-hidden />
        {t("issues.errors", { count: errors })}
        {warnings > 0 ? (
          <span className="text-ink-muted">· {t("issues.warnings", { count: warnings })}</span>
        ) : null}
      </>
    );
  } else if (warnings > 0) {
    summary = (
      <>
        <TriangleAlert className="size-4 text-warn" aria-hidden />
        {t("issues.warnings", { count: warnings })}
      </>
    );
  } else {
    summary = (
      <>
        <CircleCheck className="size-4 text-ok" aria-hidden />
        {t("issues.ok")}
      </>
    );
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button
          size="sm"
          variant="ghost"
          aria-label={`${t("issues.button")}: ${errors > 0 ? t("issues.errors", { count: errors }) : warnings > 0 ? t("issues.warnings", { count: warnings }) : t("issues.ok")}`}
        >
          {summary}
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={6}
          collisionPadding={8}
          className="z-50 flex max-h-[min(30rem,var(--radix-popover-content-available-height))] w-96 flex-col overflow-hidden rounded-md border border-line bg-surface shadow-lg"
        >
          <div className="border-b border-line px-3 py-2.5">
            <p className="text-sm font-semibold text-ink">{t("issues.title")}</p>
            <p className="mt-0.5 text-xs text-ink-muted">{t("issues.description")}</p>
          </div>
          <ul className="min-h-0 flex-1 overflow-y-auto p-1">
            {issues.length === 0 ? (
              <li className="px-2 py-4 text-center text-sm text-ink-muted">{t("issues.empty")}</li>
            ) : (
              issues.map((issue, index) => {
                const node = issue.node_id ? nodeById(graph, issue.node_id) : undefined;
                const where = node
                  ? `${node.title || node.id} (${node.id})`
                  : issue.edge_id
                    ? t("issues.edge", { id: issue.edge_id })
                    : (issue.node_id ?? t("issues.graph"));
                const Icon = issue.level === "error" ? CircleAlert : TriangleAlert;
                return (
                  <li key={issueKey(issue, index)}>
                    <button
                      type="button"
                      disabled={!node}
                      onClick={() => {
                        if (!node) return;
                        onSelectNode(node.id);
                        setOpen(false);
                      }}
                      className={cn(
                        "flex w-full items-start gap-2 rounded-sm px-2 py-1.5 text-left text-sm",
                        node ? "hover:bg-hover" : "cursor-default",
                      )}
                    >
                      <Icon
                        className={cn(
                          "mt-0.5 size-4 shrink-0",
                          issue.level === "error" ? "text-danger" : "text-warn",
                        )}
                        aria-label={
                          issue.level === "error" ? t("issues.error") : t("issues.warning")
                        }
                      />
                      <span className="min-w-0">
                        <span className="block text-ink">{issue.message}</span>
                        <span className="block truncate text-xs text-ink-muted">
                          {where}
                          {issue.field ? ` · ${t("issues.field", { field: issue.field })}` : ""}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })
            )}
          </ul>
          <div className="border-t border-line p-2">
            <Button
              size="sm"
              variant="secondary"
              loading={checking}
              onClick={() => {
                setChecking(true);
                void onRevalidate().finally(() => setChecking(false));
              }}
            >
              <RefreshCw className="size-3.5" aria-hidden />
              {t("issues.revalidate")}
            </Button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
