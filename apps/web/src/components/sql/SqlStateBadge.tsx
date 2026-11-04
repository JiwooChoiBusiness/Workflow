import { useTranslation } from "react-i18next";
import type { SqlSourceState } from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/badge";

export const STATE_TONE: Record<SqlSourceState, BadgeTone> = {
  empty: "neutral",
  not_prepared: "warn",
  stale: "warn",
  ready: "ok",
};

export function SqlStateBadge({ state }: { state: SqlSourceState }) {
  const { t } = useTranslation("sql");
  return <Badge tone={STATE_TONE[state]}>{t(`state.${state}`)}</Badge>;
}
