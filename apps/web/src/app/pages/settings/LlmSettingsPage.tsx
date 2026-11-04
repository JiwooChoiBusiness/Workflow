import { CircleCheck, CircleX, PlugZap } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type {
  EmbeddingDailyUsage,
  EmbeddingLimits,
  LlmKeyStatus,
  LlmStatus,
  LlmTestResult,
} from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { KeyValueList, PageHeader, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useEmbeddingUsage, useLlmStatus, useLlmTest } from "@/features/settings/api";
import { formatDateTime, formatNumber, formatTime } from "@/lib/format";

function KeyState({ entry }: { entry: LlmKeyStatus }) {
  const { t } = useTranslation("settings");
  if (entry.disabled) return <Badge tone="danger">{t("llm.keyState.disabled")}</Badge>;
  const exhausted = Object.keys(entry.exhausted_by_model ?? {}).length;
  if (exhausted > 0) {
    return <Badge tone="warn">{t("llm.keyState.exhausted", { count: exhausted })}</Badge>;
  }
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

// 유료 키(3단계 Z1, 14.4): 끝 4자리와 상태. 무료 키 목록에 같은 키가 있으면 서버가 뜨지 않으므로
// 여기 보이는 유료 키는 늘 새 프로젝트 검사를 통과한 것이다.
function PaidKeyPanel({ data }: { data: LlmStatus }) {
  const { t } = useTranslation("settings");
  const paid = data.paid_key;
  return (
    <Panel title={t("llm.paid.title")} description={t("llm.paid.newProject")}>
      {paid ? (
        <KeyValueList
          items={[
            {
              label: t("llm.paid.key"),
              value: (
                <span className="flex items-center gap-2">
                  <span className="font-mono text-xs">…{paid.last4}</span>
                  <KeyState entry={paid} />
                </span>
              ),
            },
          ]}
        />
      ) : (
        <p className="text-sm text-ink-muted">{t("llm.paid.none")}</p>
      )}
    </Panel>
  );
}

function limitCell(value: number, off: string) {
  return value > 0 ? formatNumber(value) : off;
}

// 임베딩 리미터(3단계 6.6): 등급별 분당·하루 몫과 오늘 사용량. 값은 지식 기본값 API가 주는 지금 돌고
// 있는 리미터의 것이다(지식 화면과 같은 출처).
function EmbeddingPanel() {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const usage = useEmbeddingUsage();
  const panel = (body: ReactNode) => (
    <Panel title={t("llm.embedding.title")} description={t("llm.embedding.description")}>
      {body}
    </Panel>
  );
  if (usage.isPending) return panel(<PageSpinner label={tc("state.loading")} />);
  if (usage.isError) {
    return panel(<ErrorAlert error={usage.error} onRetry={() => void usage.refetch()} />);
  }
  const embedding = usage.data.limiter;
  const daily = new Map(usage.data.daily.map((item) => [item.tier, item]));
  const rows: { tier: "free" | "paid"; limits: EmbeddingLimits; day?: EmbeddingDailyUsage }[] = [
    { tier: "free", limits: embedding.free, day: daily.get("free") },
    { tier: "paid", limits: embedding.paid, day: daily.get("paid") },
  ];
  const off = t("llm.embedding.off");
  return panel(
    <div className="flex flex-col gap-3">
      {!embedding.capped ? <Alert tone="info" title={t("llm.embedding.fake")} /> : null}
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium">{t("llm.embedding.tier")}</th>
            <th className="pb-2 text-right font-medium">{t("llm.embedding.rpm")}</th>
            <th className="pb-2 text-right font-medium">{t("llm.embedding.reserve")}</th>
            <th className="pb-2 text-right font-medium">{t("llm.embedding.bulk")}</th>
            <th className="pb-2 text-right font-medium">{t("llm.embedding.tpm")}</th>
            <th className="pb-2 text-right font-medium">{t("llm.embedding.rpd")}</th>
            <th className="pb-2 pl-4 font-medium" />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map(({ tier, limits, day }) => (
            <tr key={tier}>
              <td className="py-2">{t(`llm.embedding.${tier}`)}</td>
              <td className="py-2 text-right tabular-nums">{formatNumber(limits.rpm)}</td>
              <td className="py-2 text-right tabular-nums">{formatNumber(limits.reserve)}</td>
              <td className="py-2 text-right tabular-nums">
                {formatNumber(Math.max(0, limits.rpm - limits.reserve))}
              </td>
              <td className="py-2 text-right tabular-nums">{limitCell(limits.tpm, off)}</td>
              <td className="py-2 text-right tabular-nums">{limitCell(limits.rpd, off)}</td>
              <td className="py-2 pl-4 text-xs text-ink-muted">
                {day?.blocked_until ? (
                  <span className="text-warn">
                    {t("llm.embedding.blocked", { time: formatDateTime(day.blocked_until) })}
                  </span>
                ) : day && day.limit > 0 ? (
                  t("llm.embedding.today", {
                    used: formatNumber(day.used),
                    limit: formatNumber(day.limit),
                    time: formatDateTime(day.reset_at),
                  })
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>,
  );
}

// 일일 한도로 막힌 (키, 모델)과 풀리는 때(3단계 Z2). 채팅·임베딩이 같은 키 풀을 쓴다.
function BlockedPanel({ data }: { data: LlmStatus }) {
  const { t } = useTranslation("settings");
  const keys = [...data.keys, ...(data.paid_key ? [data.paid_key] : [])];
  const blocked = keys.flatMap((key) =>
    Object.entries(key.exhausted_by_model ?? {}).map(([model, until]) => ({ key, model, until })),
  );
  return (
    <Panel title={t("llm.blocked.title")} description={t("llm.blocked.description")}>
      {blocked.length === 0 ? (
        <p className="text-sm text-ink-muted">{t("llm.blocked.empty")}</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {blocked.map(({ key, model, until }) => (
            <li key={`${key.index}-${model}`} className="flex flex-wrap items-center gap-2">
              <Badge tone={key.tier === "paid" ? "warn" : "neutral"}>
                {t(`llm.embedding.${key.tier ?? "free"}`)} {key.index}
              </Badge>
              <span className="font-mono text-xs">…{key.last4}</span>
              <span className="font-mono">{model}</span>
              <span className="text-ink-muted">
                {t("llm.blocked.until", { time: formatDateTime(until) })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
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
      <PaidKeyPanel data={data} />
      <EmbeddingPanel />
      <BlockedPanel data={data} />
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
