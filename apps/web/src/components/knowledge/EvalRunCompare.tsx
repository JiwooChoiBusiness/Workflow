import { useTranslation } from "react-i18next";
import type { EvalRunOut } from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useEvalRun } from "@/features/knowledge/api";
import {
  changeCounts,
  compareCases,
  compareMetrics,
  formatDelta,
  formatMetric,
  settingsDifferences,
  type RankChange,
  type SettingsDifference,
} from "@/features/knowledge/evalMetrics";
import type { KT } from "@/features/knowledge/messages";
import { cn } from "@/lib/cn";
import { formatDateTime } from "@/lib/format";
import { KbErrorAlert } from "./KbErrorAlert";

const CHANGE_TONE: Record<RankChange, BadgeTone> = {
  better: "ok",
  worse: "danger",
  same: "neutral",
  added: "info",
  removed: "neutral",
};

function settingValue(item: SettingsDifference["before"], key: SettingsDifference["key"], t: KT) {
  if (typeof item === "boolean") return item ? t("eval.yes") : t("eval.no");
  if (key === "search_method") return t(`retrieval.methods.${item as "hybrid_search"}`);
  if (key === "reranking_mode") return t(`retrieval.modes.${item as "rrf"}`);
  return item === "" ? "-" : String(item);
}

function rank(value: number | null, t: KT) {
  return value === null ? t("eval.missed") : String(value);
}

function Comparison({ before, after }: { before: EvalRunOut; after: EvalRunOut }) {
  const { t } = useTranslation("knowledge");
  const metrics = compareMetrics(before.metrics, after.metrics);
  const settings = settingsDifferences(before.settings, after.settings);
  const cases = compareCases(before.cases, after.cases);
  const counts = changeCounts(cases);
  const changed = cases.filter((item) => item.change !== "same");
  return (
    <div className="flex flex-col gap-4">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium" />
            <th className="pb-2 text-right font-medium">
              {t("eval.before")}
              <span className="block font-normal">{formatDateTime(before.created_at)}</span>
            </th>
            <th className="pb-2 text-right font-medium">
              {t("eval.after")}
              <span className="block font-normal">{formatDateTime(after.created_at)}</span>
            </th>
            <th className="pb-2 text-right font-medium">{t("eval.delta")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {metrics.map((item) => (
            <tr key={item.key}>
              <td className="py-1.5 font-medium text-ink">{t(`eval.metrics.${item.key}`)}</td>
              <td className="py-1.5 text-right font-mono tabular-nums">
                {formatMetric(item.before)}
              </td>
              <td className="py-1.5 text-right font-mono tabular-nums">
                {formatMetric(item.after)}
              </td>
              <td
                className={cn(
                  "py-1.5 text-right font-mono tabular-nums",
                  item.delta > 0.0005
                    ? "text-ok"
                    : item.delta < -0.0005
                      ? "text-danger"
                      : "text-ink-muted",
                )}
              >
                {formatDelta(item.delta)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div>
        <p className="mb-1 text-xs font-medium text-ink-muted">{t("eval.settingsDiff")}</p>
        {settings.length === 0 ? (
          <p className="text-sm text-ink-muted">{t("eval.settingsSame")}</p>
        ) : (
          <ul className="flex flex-col gap-0.5 text-sm">
            {settings.map((item) => (
              <li key={item.key}>
                <span className="text-ink-muted">{t(`eval.settingKeys.${item.key}`)}</span>{" "}
                <span className="font-mono">{settingValue(item.before, item.key, t)}</span> →{" "}
                <span className="font-mono">{settingValue(item.after, item.key, t)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <p className="mb-1 text-xs font-medium text-ink-muted">
          {t("eval.changes", { better: counts.better, worse: counts.worse, same: counts.same })}
        </p>
        {changed.length > 0 ? (
          <table className="w-full text-sm">
            <tbody className="divide-y divide-line">
              {changed.map((item) => (
                <tr key={item.caseId}>
                  <td className="py-1.5 pr-3">
                    <Badge tone={CHANGE_TONE[item.change]}>
                      {t(`eval.changeLabels.${item.change}`)}
                    </Badge>
                  </td>
                  <td className="max-w-96 truncate py-1.5 pr-3" title={item.question}>
                    {item.question}
                  </td>
                  <td className="py-1.5 text-right font-mono text-xs whitespace-nowrap tabular-nums">
                    {rank(item.before, t)} → {rank(item.after, t)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </div>
  );
}

// 저장한 평가 실행 둘을 나란히 비교한다(설계 7.7 확장). 이전은 먼저 실행한 쪽이다.
export function EvalRunCompare({ kbId, runIds }: { kbId: string; runIds: [string, string] }) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const first = useEvalRun(kbId, runIds[0]);
  const second = useEvalRun(kbId, runIds[1]);
  let body;
  if (first.isPending || second.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (first.isError || second.isError) {
    body = <KbErrorAlert error={first.error ?? second.error} />;
  } else {
    const [before, after] =
      first.data.created_at <= second.data.created_at
        ? [first.data, second.data]
        : [second.data, first.data];
    body = <Comparison before={before} after={after} />;
  }
  return <Panel title={t("eval.compareTitle")}>{body}</Panel>;
}
