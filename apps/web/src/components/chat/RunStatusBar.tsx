import { useTranslation } from "react-i18next";
import { useShallow } from "zustand/react/shallow";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { formatDuration, formatNumber, formatUsd } from "@/lib/format";
import { secondClock, useNow } from "@/lib/useNow";
import { isRunActive, waitingChildName, type RunStatus, type UsageTotals } from "@/stores/runStore";
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

export function LiveElapsed({ start }: { start: number }) {
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

export interface RunStatusValues {
  status: RunStatus;
  // 에이전트 런은 지금 스텝, 워크플로우 런은 끝난 노드 수
  step: number;
  stepLimit: number | null;
  usage: UsageTotals;
  model: string | null;
  queuePosition: number | null;
  startedAt: string | null;
  endedAt: string | null;
  // 에이전트 런은 기다리는 하위 에이전트, 워크플로우 런은 지금 도는 노드의 이름
  activity: string | null;
}

// 런 상태 한 줄(설계 7.5 RunStatusBar). kind에 따라 스텝·노드 표시와 이름표만 다르다.
export function RunStatusView({
  kind = "agent",
  values,
}: {
  kind?: "agent" | "workflow";
  values: RunStatusValues;
}) {
  const { t } = useTranslation("chat");
  if (values.status === "idle") return null;
  const active = isRunActive(values.status);
  const statusLabel =
    values.status === "queued"
      ? values.queuePosition
        ? t("status.queued", { position: values.queuePosition })
        : t("status.queuedNoPosition")
      : t(`status.${values.status}`);
  const workflow = kind === "workflow";

  return (
    <div
      role="status"
      aria-label={workflow ? t("statusBar.workflowLabel") : t("statusBar.label")}
      className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted"
    >
      <Badge tone={STATUS_TONE[values.status]}>
        {active ? <Spinner className="size-3" /> : null}
        {statusLabel}
      </Badge>
      {values.activity ? (
        <span className="text-ink">
          {workflow
            ? t("statusBar.node", { name: values.activity })
            : t("statusBar.child", { name: values.activity })}
        </span>
      ) : null}
      {values.step > 0 ? (
        <span className="tabular-nums">
          {workflow
            ? t("statusBar.nodes", { count: values.step })
            : values.stepLimit
              ? t("statusBar.step", { step: values.step, limit: values.stepLimit })
              : t("statusBar.stepNoLimit", { step: values.step })}
        </span>
      ) : null}
      {values.usage.inputTokens + values.usage.outputTokens > 0 ? (
        <span className="tabular-nums">
          {t("statusBar.tokens", {
            input: formatNumber(values.usage.inputTokens),
            output: formatNumber(values.usage.outputTokens),
          })}
          {values.usage.estimated ? ` (${t("statusBar.estimated")})` : ""}
        </span>
      ) : null}
      {values.usage.costUsd !== null ? (
        <span className="tabular-nums">
          {t("statusBar.cost", { cost: formatUsd(values.usage.costUsd) })}
        </span>
      ) : null}
      {values.startedAt && (active || values.endedAt) ? (
        <span className="tabular-nums">
          {t("statusBar.elapsed")}{" "}
          <Elapsed startedAt={values.startedAt} endedAt={values.endedAt} live={active} />
        </span>
      ) : null}
      {values.model ? <span className="font-mono">{values.model}</span> : null}
    </div>
  );
}

// 채팅 패널의 에이전트 런 상태. runStore에서 읽는다.
export function RunStatusBar() {
  const values = useRunStore(
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
  const childName = useRunStore(waitingChildName);
  return <RunStatusView values={{ ...values, activity: childName }} />;
}
