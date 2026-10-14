import { useCallback, useEffect, useRef, useState } from "react";
import { isApiError } from "@/api/http";
import type { WorkflowDraftUpdate } from "@/api/types";
import { fetchDraft, saveDraft } from "./api";
import { isDirty, type FlowState, type FlowStore } from "./flowStore";
import { toGraphPayload } from "./graph";

// 마지막 변경 뒤 이만큼 조용하면 저장한다(설계 7.3).
export const AUTOSAVE_DELAY_MS = 1500;

export type SaveStatus = "saved" | "dirty" | "saving" | "failed" | "conflict";

export function draftPayload(state: FlowState): WorkflowDraftUpdate {
  return {
    graph: { ...toGraphPayload(state.graph), viewport: state.viewport },
    env_vars: state.envVars.map((row) => ({
      name: row.name.trim(),
      secret: row.secret,
      // 서버는 value가 null이면 같은 이름으로 저장된 값을 그대로 둔다.
      value: row.keep && row.value === "" ? null : row.value,
    })),
    conversation_vars: state.kind === "chatflow" ? state.conversationVars : [],
    settings: state.settings,
    updated_at: state.updatedAt,
  };
}

export interface Autosave {
  status: SaveStatus;
  error: unknown;
  // 기다리지 않고 바로 저장한다. 저장할 것이 없거나 성공하면 true.
  flush: () => Promise<boolean>;
  // 충돌 뒤 서버의 최신 초안으로 다시 시작한다.
  reload: () => Promise<void>;
}

export function useDraftAutosave(store: FlowStore, workflowId: string): Autosave {
  const [status, setStatus] = useState<SaveStatus>("saved");
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflight = useRef<Promise<boolean> | null>(null);
  const conflict = useRef(false);

  const save = useCallback(async (): Promise<boolean> => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    while (inflight.current) await inflight.current;
    if (conflict.current) return false;
    const state = store.getState();
    if (!isDirty(state)) return true;
    const revision = state.revision;
    setStatus("saving");
    const request = saveDraft(workflowId, draftPayload(state)).then(
      (saved) => {
        store.getState().actions.markSaved(revision, saved);
        setError(null);
        setStatus(isDirty(store.getState()) ? "dirty" : "saved");
        return true;
      },
      (failure: unknown) => {
        if (isApiError(failure, 409) && failure.code === "draft_conflict") {
          conflict.current = true;
          setStatus("conflict");
        } else {
          setError(failure);
          setStatus("failed");
        }
        return false;
      },
    );
    inflight.current = request;
    try {
      return await request;
    } finally {
      inflight.current = null;
    }
  }, [store, workflowId]);

  // 저장하는 동안 또 바뀌면 구독이 타이머를 다시 건다. 그 저장은 앞 저장이 끝나기를 기다린다.
  const schedule = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      void save();
    }, AUTOSAVE_DELAY_MS);
  }, [save]);

  useEffect(() => {
    const unsubscribe = store.subscribe((state, previous) => {
      if (state.revision === previous.revision || conflict.current) return;
      if (isDirty(state)) {
        setStatus((current) => (current === "saving" ? current : "dirty"));
        schedule();
      }
    });
    return () => {
      unsubscribe();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [store, schedule]);

  const reload = useCallback(async () => {
    const draft = await fetchDraft(workflowId);
    if (timer.current) clearTimeout(timer.current);
    conflict.current = false;
    store.getState().actions.load(draft);
    setError(null);
    setStatus("saved");
  }, [store, workflowId]);

  return { status, error, flush: save, reload };
}
