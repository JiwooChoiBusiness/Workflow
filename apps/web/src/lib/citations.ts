/*
 * 인용(3단계 설계 8.3, 8.4). 메시지·런 저장본(REST RetrieverResourceData)과 이벤트(run-events.gen의 같은
 * 이름)가 모양이 거의 같아 화면은 둘 다 받는 부분 모양만 쓴다. position은 런 전체에서 세그먼트마다 하나이고
 * 답의 [n]과 같다. 저장본은 내용을 2,000자까지, 이벤트는 500자까지만 갖고 있다.
 */
export interface Citation {
  position: number;
  dataset_id: string;
  dataset_name: string;
  document_id: string;
  document_name: string;
  segment_id: string;
  segment_position: number;
  score: number;
  content: string;
  answer?: string | null;
  // 맥락 확장(hierarchical_model) 문서에서 맞은 작은 조각. 글은 저장본·이벤트 모두 500자까지다.
  child_chunks?: readonly { id: string; content: string; position: number; score: number }[] | null;
}

export const NO_CITATIONS: readonly Citation[] = [];

// 같은 세그먼트는 먼저 온 것만 두고 position 순으로 놓는다.
export function mergeCitations(
  current: readonly Citation[],
  incoming: readonly Citation[],
): Citation[] {
  const seen = new Set(current.map((item) => item.segment_id));
  const merged = [...current];
  for (const item of incoming) {
    if (seen.has(item.segment_id)) continue;
    seen.add(item.segment_id);
    merged.push(item);
  }
  return merged.sort((a, b) => a.position - b.position);
}

interface MessageLike {
  role: string;
  run_id?: string | null;
  retriever_resources?: readonly Citation[] | null;
}

/*
 * 에이전트 런의 인용은 런이 끝나면 그 런의 마지막 assistant 메시지 하나로 모인다(17.4 M24 이후 확정). 입력을
 * 기다리는 런은 중간 스텝 메시지에도 인용이 남아 있으므로 마지막이 아닌 메시지의 것은 버린다. 런 id → 그 런의
 * 마지막 assistant 메시지 id다.
 */
export function lastAssistantByRun<M extends MessageLike & { id: string }>(
  messages: readonly M[],
): Map<string, string> {
  const last = new Map<string, string>();
  for (const message of messages) {
    if (message.role === "assistant" && message.run_id) last.set(message.run_id, message.id);
  }
  return last;
}

export function runCitations<M extends MessageLike & { id: string }>(
  messages: readonly M[],
): Record<string, Citation[]> {
  const last = lastAssistantByRun(messages);
  const found: Record<string, Citation[]> = {};
  for (const message of messages) {
    const runId = message.run_id;
    if (!runId || last.get(runId) !== message.id) continue;
    const resources = message.retriever_resources ?? [];
    if (resources.length > 0) found[runId] = mergeCitations([], resources);
  }
  return found;
}

export function sameCitations(a: readonly Citation[], b: readonly Citation[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  return a.every(
    (item, index) =>
      item.segment_id === b[index]?.segment_id &&
      item.position === b[index]?.position &&
      item.content === b[index]?.content,
  );
}
