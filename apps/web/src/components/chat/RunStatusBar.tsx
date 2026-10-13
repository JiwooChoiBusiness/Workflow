import { useTranslation } from "react-i18next";
import { useShallow } from "zustand/react/shallow";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { formatDuration, formatNumber, formatUsd } from "@/lib/format";
import { secondClock, useNow } from "@/lib/useNow";
import { isRunActive, type RunStatus } from "@/stores/runStore";
import { useRunStore } from "@/stores/runStoreContext";

const STATUS_TONE: Record<RunStatus, BadgeTone> = {
  idle: "neutral",
  queued: "info",
  running: "info",
  waiting_input: "warn",
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
};

function LiveElapsed({ start }: { start: number }) {
  const now = useNow(secondClock);
  return <>{formatDuration(Math.max(0, now - start))}</>;
}

function Elapsed({
  startedAt,
  endedAt,
  live,
}: {
  startedAt: string;
  endedAt: string | null;
  live: boolean;
}) {
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;
  if (live) return <LiveElapsed start={start} />;
  if (!endedAt) return null;
  return <>{formatDuration(Math.max(0, new Date(endedAt).getTime() - start))}</>;
}

export function RunStatusBar() {
  const { t } = useTranslation("chat");
  const state = useRunStore(
    useShallow((s) => ({
      status: s.status,
      step: s.step,
      stepLimit: s.stepLimit,
      usage: s.usage,
      model: s.model,
      queuePosition: s.queuePosition,
      startedAt: s.startedAt,
      endedAt: s.endedAt,
    })),
  );
  if (state.status === "idle") return null;
  const active = isRunActive(state.status);
  const statusLabel =
    state.status === "queued"
      ? state.queuePosition
        ? t("status.queued", { position: state.queuePosition })
        : t("status.queuedNoPosition")
      : t(`status.${state.status}`);

  return (
    <div
      role="status"
      aria-label={t("statusBar.label")}
      className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted"
    >
      <Badge tone={STATUS_TONE[state.status]}>
        {active ? <Spinner className="size-3" /> : null}
        {statusLabel}
      </Badge>
      {state.step > 0 ? (
        <span className="tabular-nums">
          {state.stepLimit
            ? t("statusBar.step", { step: state.step, limit: state.stepLimit })
            : t("statusBar.stepNoLimit", { step: state.step })}
        </span>
      ) : null}
      {state.usage.inputTokens + state.usage.outputTokens > 0 ? (
        <span className="tabular-nums">
          {t("statusBar.tokens", {
            input: formatNumber(state.usage.inputTokens),
            output: formatNumber(state.usage.outputTokens),
          })}
          {state.usage.estimated ? ` (${t("statusBar.estimated")})` : ""}
        </span>
      ) : null}
      {state.usage.costUsd !== null ? (
        <span className="tabular-nums">
          {t("statusBar.cost", { cost: formatUsd(state.usage.costUsd) })}
        </span>
      ) : null}
      {state.startedAt && (active || state.endedAt) ? (
        <span className="tabular-nums">
          {t("statusBar.elapsed")}{" "}
          <Elapsed startedAt={state.startedAt} endedAt={state.endedAt} live={active} />
        </span>
      ) : null}
      {state.model ? <span className="font-mono">{state.model}</span> : null}
    </div>
  );
}
