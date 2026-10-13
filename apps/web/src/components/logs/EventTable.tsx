import { useState } from "react";
import { useTranslation } from "react-i18next";
import { prettyJson } from "@/components/chat/toolSummary";
import { Input } from "@/components/ui/input";
import type { RunEventOut } from "@/features/logs/types";

const timeFormat = new Intl.DateTimeFormat("ko-KR", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  fractionalSecondDigits: 3,
  hour12: false,
});

function eventTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : timeFormat.format(date);
}

function preview(data: RunEventOut["data"]): string {
  const text = JSON.stringify(data);
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

// 원본 이벤트 표(설계 7.3 EventTable). SQLite에 남은 영속 이벤트만 있다. 델타는 저장하지 않는다.
export function EventTable({ events, truncated }: { events: RunEventOut[]; truncated: boolean }) {
  const { t } = useTranslation("logs");
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? events.filter((event) => event.type.toLowerCase().includes(needle))
    : events;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Input
          aria-label={t("events.filter")}
          placeholder={t("events.filterPlaceholder")}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          className="max-w-60"
        />
        <span className="text-xs text-ink-muted">
          {t("events.count", { shown: shown.length, total: events.length })}
        </span>
      </div>
      {truncated ? <p className="text-xs text-warn">{t("events.truncated")}</p> : null}
      <div className="overflow-x-auto rounded-md border border-line">
        <table className="w-full text-xs">
          <thead className="bg-sunken text-left text-ink-muted">
            <tr>
              <th className="px-2 py-1.5 text-right font-medium">{t("events.seq")}</th>
              <th className="px-2 py-1.5 font-medium">{t("events.time")}</th>
              <th className="px-2 py-1.5 font-medium">{t("events.type")}</th>
              <th className="px-2 py-1.5 font-medium">{t("events.data")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {shown.map((event) => (
              <tr key={event.seq} className="align-top">
                <td className="px-2 py-1.5 text-right text-ink-subtle tabular-nums">{event.seq}</td>
                <td className="px-2 py-1.5 whitespace-nowrap text-ink-muted tabular-nums">
                  <time dateTime={event.ts}>{eventTime(event.ts)}</time>
                </td>
                <td className="px-2 py-1.5 font-mono whitespace-nowrap text-ink">{event.type}</td>
                <td className="w-full px-2 py-1.5">
                  <details>
                    <summary className="cursor-pointer font-mono break-all text-ink-muted select-none">
                      {preview(event.data)}
                    </summary>
                    <pre className="mt-1 max-h-80 overflow-auto rounded-sm bg-code px-2 py-1.5 font-mono whitespace-pre-wrap text-code-ink">
                      {prettyJson(event.data)}
                    </pre>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
