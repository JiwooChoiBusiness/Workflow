import { formatRelative } from "@/lib/format";

// 화면의 시계(useNow)는 30초마다 바뀌므로 방금 기록된 시각이 "27초 후"처럼 보일 수 있다. 기록은 모두
// 지난 일이라 지금보다 늦은 시각은 지금으로 친다.
export function formatPast(iso: string | null | undefined, now: number): string {
  if (!iso) return formatRelative(iso, now);
  const at = new Date(iso).getTime();
  return formatRelative(iso, Number.isNaN(at) ? now : Math.max(now, at));
}
