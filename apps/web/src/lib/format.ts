const LOCALE = "ko-KR";

const integer = new Intl.NumberFormat(LOCALE);
const dateTime = new Intl.DateTimeFormat(LOCALE, { dateStyle: "medium", timeStyle: "short" });
const time = new Intl.DateTimeFormat(LOCALE, { timeStyle: "medium" });
const relative = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto" });

function unit(value: number, unitName: string, fractionDigits = 0): string {
  return new Intl.NumberFormat(LOCALE, {
    style: "unit",
    unit: unitName,
    unitDisplay: "short",
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : integer.format(value);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "-";
  // Intl의 byte 단위는 "81byte"처럼 나와 KB·MB와 어울리지 않는다.
  if (bytes < 1024) return `${integer.format(bytes)}B`;
  if (bytes < 1024 ** 2) return unit(bytes / 1024, "kilobyte", 1);
  if (bytes < 1024 ** 3) return unit(bytes / 1024 ** 2, "megabyte", 1);
  return unit(bytes / 1024 ** 3, "gigabyte", 2);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "-";
  if (ms < 1000) return unit(Math.round(ms), "millisecond");
  const seconds = ms / 1000;
  if (seconds < 60) return unit(seconds, "second", 1);
  const totalSeconds = Math.floor(seconds);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${unit(minutes, "minute")} ${unit(totalSeconds % 60, "second")}`;
  return `${unit(Math.floor(minutes / 60), "hour")} ${unit(minutes % 60, "minute")}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : dateTime.format(date);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : time.format(date);
}

export function formatRelative(iso: string | null | undefined, now: number): string {
  if (!iso) return "-";
  const date = new Date(iso).getTime();
  if (Number.isNaN(date)) return iso;
  const seconds = Math.round((date - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 60) return relative.format(seconds, "second");
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  return relative.format(Math.round(seconds / 86400), "day");
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return value < 0.01 ? value.toFixed(4) : value.toFixed(2);
}
