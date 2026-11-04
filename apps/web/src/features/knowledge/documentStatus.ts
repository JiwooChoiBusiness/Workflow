/*
 * 지식 문서의 상태(설계 4.4)를 화면에 맞게 읽는다. 목록 배지, 진행률, 일일 한도로 멈춘 문서의 다시 시도
 * 안내, 일괄 조작을 쓸 수 있는지, 진행 중 문서가 있을 때만 도는 2초 폴링을 정한다.
 */
import type { DisplayStatus, IndexingStatus, KbDocumentOut } from "@/api/types";
import type { BadgeTone } from "@/components/ui/badge";

export const POLL_MS = 2000;

// 상대 시각의 기준. 30초마다 도는 시계(useNow)보다 방금 생긴 행이 앞서면 "3초 후"처럼 보여서 미래 시각은
// 지금으로 본다.
export function relativeBase(iso: string, now: number): number {
  const at = Date.parse(iso);
  return Number.isNaN(at) ? now : Math.max(now, at);
}

export const DISPLAY_STATUSES: readonly DisplayStatus[] = [
  "queuing",
  "indexing",
  "paused",
  "error",
  "available",
  "disabled",
  "archived",
];

const IN_PROGRESS: ReadonlySet<IndexingStatus> = new Set([
  "waiting",
  "parsing",
  "cleaning",
  "splitting",
  "indexing",
]);

const TONES: Record<DisplayStatus, BadgeTone> = {
  queuing: "neutral",
  indexing: "info",
  paused: "warn",
  error: "danger",
  available: "ok",
  disabled: "neutral",
  archived: "neutral",
};

export function statusTone(status: DisplayStatus): BadgeTone {
  return TONES[status];
}

type StatusFields = Pick<KbDocumentOut, "indexing_status">;

export function isInProgress(document: StatusFields): boolean {
  return IN_PROGRESS.has(document.indexing_status);
}

// 임베딩 단계에서만 뜻이 있다. 그 전에는 청크 수가 아직 정해지지 않았다.
export function progressPercent(
  document: Pick<KbDocumentOut, "indexing_status" | "total_segments" | "completed_segments">,
): number | null {
  if (document.indexing_status !== "indexing" || document.total_segments <= 0) return null;
  return Math.min(100, Math.floor((document.completed_segments / document.total_segments) * 100));
}

export interface DocumentError {
  code: string;
  message: string;
  retryable: boolean;
  // 일일 한도(rate_limited_daily)가 풀리는 때. 그 전에 다시 시도하면 서버가 안내만 돌려준다.
  retryAfter: Date | null;
}

function parseDate(value: unknown): Date | null {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function documentError(document: Pick<KbDocumentOut, "error">): DocumentError | null {
  const error = document.error;
  if (!error) return null;
  return {
    code: typeof error.code === "string" ? error.code : "unknown",
    message: typeof error.message === "string" ? error.message : "",
    retryable: error.retryable === true,
    retryAfter: parseDate(error.retry_after),
  };
}

export function isDailyLimited(document: Pick<KbDocumentOut, "error">): boolean {
  return documentError(document)?.code === "rate_limited_daily";
}

// 오류 문서를 지금 다시 시도해도 되는지. 일일 한도는 풀리는 시각 전이면 안 된다.
export function canRetryNow(
  document: Pick<KbDocumentOut, "indexing_status" | "error">,
  now: number,
): boolean {
  if (document.indexing_status !== "error") return false;
  const retryAfter = documentError(document)?.retryAfter;
  return !retryAfter || retryAfter.getTime() <= now;
}

// 올린 묶음(indexing-status) 안에 진행 중 문서가 있으면 2초마다 다시 읽는다. 아직 받지 않았거나 다
// 끝났으면 멈춘다.
export function batchPollInterval(items: readonly StatusFields[] | undefined): number | false {
  return items?.some(isInProgress) ? POLL_MS : false;
}

// 진행 중 문서가 목록에 있거나 KB가 대기·색인 중 문서를 세고 있으면 2초마다 다시 읽는다. 다 끝나면 멈춘다.
export function documentsPollInterval(
  items: readonly StatusFields[] | undefined,
  kbInProgress = 0,
): number | false {
  if (kbInProgress > 0) return POLL_MS;
  return batchPollInterval(items);
}

export type DocumentAction =
  "enable" | "disable" | "archive" | "unarchive" | "pause" | "resume" | "retry";

export interface ActionState {
  enabled: boolean;
  // 막힌 이유(번역 키의 끝 부분). 고른 문서 중 맞는 것이 없거나 색인 중 문서가 섞였을 때다.
  reason?: "none" | "indexing";
}

// 서버 규칙(knowledge_documents.set_status): 켜기·끄기·보관은 고른 문서 중 하나라도 색인 중이면 400이고,
// 나머지 조작은 맞지 않는 문서를 skipped로 돌려준다. 화면은 맞는 문서가 하나라도 있을 때만 단추를 연다.
export function actionStates(
  documents: readonly Pick<
    KbDocumentOut,
    "indexing_status" | "enabled" | "archived" | "display_status"
  >[],
): Record<DocumentAction, ActionState> {
  const busy = documents.some(isInProgress);
  const some = (predicate: (document: (typeof documents)[number]) => boolean) =>
    documents.some(predicate);
  const nothing: ActionState = { enabled: false, reason: "none" };
  const plain = (match: boolean): ActionState => (match ? { enabled: true } : nothing);
  const guarded = (match: boolean): ActionState =>
    busy ? { enabled: false, reason: "indexing" } : plain(match);
  const settled = (document: (typeof documents)[number]) => !isInProgress(document);
  return {
    enable: guarded(some((d) => settled(d) && !d.enabled && !d.archived)),
    disable: guarded(some((d) => settled(d) && d.enabled && !d.archived)),
    archive: guarded(some((d) => settled(d) && !d.archived)),
    unarchive: guarded(some((d) => d.archived)),
    pause: plain(busy),
    resume: plain(some((d) => d.indexing_status === "paused")),
    retry: plain(some((d) => d.indexing_status === "error" || d.indexing_status === "waiting")),
  };
}
