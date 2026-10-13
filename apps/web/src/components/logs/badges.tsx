import { useTranslation } from "react-i18next";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import type { RunStatus, SessionSource } from "@/features/logs/types";

const STATUS_TONE: Record<RunStatus, BadgeTone> = {
  queued: "info",
  running: "info",
  waiting_input: "warn",
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
};

const SOURCE_TONE: Record<SessionSource, BadgeTone> = {
  preview: "neutral",
  webapp: "accent",
  api: "info",
  task: "warn",
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const { t } = useTranslation("logs");
  return (
    <Badge tone={STATUS_TONE[status]}>
      {status === "running" || status === "queued" ? <Spinner className="size-3" /> : null}
      {t(`runStatus.${status}`)}
    </Badge>
  );
}

export function SourceBadge({ source }: { source: SessionSource }) {
  const { t } = useTranslation("logs");
  return <Badge tone={SOURCE_TONE[source]}>{t(`sources.${source}`)}</Badge>;
}
