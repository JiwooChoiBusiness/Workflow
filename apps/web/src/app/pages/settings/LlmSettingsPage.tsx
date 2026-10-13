import { CircleCheck, CircleX, PlugZap } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { LlmKeyStatus, LlmTestResult } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { KeyValueList, PageHeader, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useLlmStatus, useLlmTest } from "@/features/settings/api";
import { formatDateTime, formatNumber, formatTime } from "@/lib/format";

function KeyState({ entry }: { entry: LlmKeyStatus }) {
  const { t } = useTranslation("settings");
  if (entry.disabled) return <Badge tone="danger">{t("llm.keyState.disabled")}</Badge>;
  if (entry.cooldown_until) {
    const models = Object.entries(entry.cooldown_by_model ?? {})
      .map(([model, until]) => `${model}: ${formatTime(until)}`)
      .join("\n");
    return (
      <Badge tone="warn" title={models || undefined}>
        {t("llm.keyState.cooldown", { time: formatTime(entry.cooldown_until) })}
      </Badge>
    );
  }
  return <Badge tone="ok">{t("llm.keyState.ok")}</Badge>;
}

function TestResult({ result }: { result: LlmTestResult }) {
  const { t } = useTranslation("settings");
  const generation = result.generation;
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex items-center gap-2">
        {result.ok ? (
          <CircleCheck className="size-5 text-ok" aria-hidden />
        ) : (
          <CircleX className="size-5 text-danger" aria-hidden />
        )}
        <span className="font-medium text-ink">
          {result.fake ? t("llm.testFake") : result.ok ? t("llm.testOk") : t("llm.testFailed")}
        </span>
        <Badge>{t("llm.modelCount", { count: result.model_count })}</Badge>
      </div>
      {result.error ? (
        <Alert tone="danger" title={result.error.code}>
          {result.error.message}
        </Alert>
      ) : null}
      {result.attempts.length > 0 ? (
        <div>
          <p className="mb-1 text-xs font-medium text-ink-muted">{t("llm.attempts")}</p>
          <ul className="flex flex-col gap-1">
            {result.attempts.map((attempt, index) => (
              <li key={index} className="flex items-center gap-2 text-xs text-ink">
                <span className={attempt.ok ? "text-ok" : "text-danger"}>●</span>
                {t("llm.attempt", {
                  index: attempt.key_index,
                  last4: attempt.key_last4,
                  status: attempt.status_code ?? "-",
                  latency: attempt.latency_ms,
                })}
                {attempt.error ? (
                  <span className="text-ink-muted">· {attempt.error.code}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {generation ? (
        <div>
          <p className="mb-1 text-xs font-medium text-ink-muted">{t("llm.generation")}</p>
          <p className="text-xs text-ink">
            {t("llm.generationDetail", {
              model: generation.model ?? "-",
              index: generation.key_index ?? "-",
              latency: generation.latency_ms,
            })}
            {generation.usage
              ? ` · ${t("llm.usage", {
                  input: formatNumber(generation.usage.input_tokens),
                  output: formatNumber(generation.usage.output_tokens),
                })}`
              : ""}
          </p>
          {generation.text ? (
            <p className="mt-1 rounded-md bg-code px-2.5 py-1.5 text-xs text-code-ink">
              <span className="text-ink-muted">{t("llm.reply")}: </span>
              {generation.text}
            </p>
          ) : null}
          {generation.error ? (
            <p className="mt-1 text-xs text-danger">
              {generation.error.code}: {generation.error.message}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function LlmSettingsPage() {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const status = useLlmStatus();
  const test = useLlmTest();

  if (status.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (status.isError) {
    return (
      <ErrorAlert
        error={status.error}
        title={t("llm.loadFailed")}
        onRetry={() => void status.refetch()}
      />
    );
  }
  const data = status.data;

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title={t("llm.title")} description={t("llm.description")} className="mb-0" />
      {data.fake ? <Alert tone="info" title={t("llm.fake")} /> : null}
      <Panel title={t("llm.overview")}>
        <KeyValueList
          items={[
            {
              label: t("llm.baseUrl"),
              value: <span className="font-mono text-xs">{data.base_url}</span>,
            },
            { label: t("llm.keyCount"), value: data.key_count },
            {
              label: t("llm.cooldown"),
              value: t("llm.cooldownValue", { seconds: data.key_cooldown_sec }),
            },
            {
              label: t("llm.fallback"),
              value: (
                <ol className="flex flex-wrap items-center gap-1.5">
                  {data.fallback_models.map((model, index) => (
                    <li key={model} className="flex items-center gap-1.5">
                      {index > 0 ? <span className="text-ink-subtle">→</span> : null}
                      <Badge>{model}</Badge>
                    </li>
                  ))}
                </ol>
              ),
            },
          ]}
        />
      </Panel>
      <Panel title={t("llm.keys")} description={t("llm.keysHint")}>
        {data.keys.length === 0 ? (
          <Alert tone="warn" title={t("llm.noKeys")} />
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-ink-muted">
              <tr>
                <th className="pb-2 font-medium">{t("llm.column.index")}</th>
                <th className="pb-2 font-medium">{t("llm.column.last4")}</th>
                <th className="pb-2 font-medium">{t("llm.column.state")}</th>
                <th className="pb-2 text-right font-medium">{t("llm.column.success")}</th>
                <th className="pb-2 text-right font-medium">{t("llm.column.failure")}</th>
                <th className="pb-2 text-right font-medium">{t("llm.column.rateLimited")}</th>
                <th className="pb-2 pl-4 font-medium">{t("llm.column.last429")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.keys.map((entry) => (
                <tr key={entry.index}>
                  <td className="py-2">{entry.index}</td>
                  <td className="py-2 font-mono text-xs">…{entry.last4}</td>
                  <td className="py-2">
                    <KeyState entry={entry} />
                  </td>
                  <td className="py-2 text-right tabular-nums">{entry.success_count ?? 0}</td>
                  <td className="py-2 text-right tabular-nums">{entry.failure_count ?? 0}</td>
                  <td className="py-2 text-right tabular-nums">{entry.rate_limited_count ?? 0}</td>
                  <td className="py-2 pl-4 text-xs text-ink-muted">
                    {entry.last_429_at ? formatDateTime(entry.last_429_at) : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      <Panel
        title={t("llm.test")}
        actions={
          <Button
            variant="primary"
            size="sm"
            loading={test.isPending}
            onClick={() => test.mutate()}
          >
            <PlugZap className="size-3.5" aria-hidden />
            {t("llm.test")}
          </Button>
        }
      >
        {test.isError ? <ErrorAlert error={test.error} /> : null}
        {test.data ? <TestResult result={test.data} /> : null}
        {!test.data && !test.isError ? (
          <p className="text-sm text-ink-muted">{t("llm.testHint")}</p>
        ) : null}
      </Panel>
    </div>
  );
}
