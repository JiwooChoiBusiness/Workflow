import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import type { InteractionInput, RunEvent } from "@/api/run-events";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { WorkspacePanel } from "@/components/chat/WorkspacePanel";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import i18n from "@/i18n";
import { cn } from "@/lib/cn";
import devStrings from "@/locales/ko/dev.json";
import { createRunStore, type RunStore } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { FixturePlayer } from "./fixturePlayer";

// 이 화면은 개발 서버에서만 묶이므로 번역도 여기서 넣는다.
i18n.addResourceBundle("ko", "dev", devStrings, true, true);

const files = import.meta.glob<RunEvent[]>("../test/fixtures/run-events/*.json", {
  eager: true,
  import: "default",
});

function fixture(name: string): RunEvent[] {
  const events = files[`../test/fixtures/run-events/${name}.json`];
  if (!events) throw new Error(`픽스처가 없다: ${name}`);
  return events;
}

interface Scenario {
  id: "text" | "approval" | "question" | "edge";
  userText: string;
  start: string;
  approve?: string;
  reject?: string;
  answer?: string;
}

const SCENARIOS: Scenario[] = [
  {
    id: "approval",
    userText: "작업공간 파일을 보고 pypi.org 상태 코드를 알려줘",
    start: "approval-wait",
    approve: "approval-approved",
    reject: "approval-rejected",
  },
  { id: "text", userText: "작업공간 폴더 구조를 설명해 줘", start: "text-only" },
  {
    id: "question",
    userText: "판매 데이터를 정리해서 저장해 줘",
    start: "question-wait",
    answer: "question-answered",
  },
  { id: "edge", userText: "notes.md와 app.db를 비교해 줘", start: "edge-cases" },
];

const SPEEDS = [
  { key: "slow", value: 0.5 },
  { key: "normal", value: 1 },
  { key: "fast", value: 4 },
  { key: "instant", value: 0 },
] as const;

// 같은 런 id가 다시 오면 스토어가 중복으로 보고 버리므로, 이어 보내는 메시지는 id를 바꿔 재생한다.
function renamed(events: RunEvent[], suffix: string): RunEvent[] {
  return events.map((event) => {
    const copy = structuredClone(event) as RunEvent & { data?: Record<string, unknown> };
    if (copy.type !== "ping") copy.run_id = `${copy.run_id}-${suffix}`;
    const data = copy.data as Record<string, unknown> | undefined;
    if (data && typeof data.message_id === "string")
      data.message_id = `${data.message_id}-${suffix}`;
    return copy;
  });
}

function withAnswer(events: RunEvent[], answer: string): RunEvent[] {
  const escaped = JSON.stringify(answer).slice(1, -1);
  return JSON.parse(JSON.stringify(events).replaceAll("{{answer}}", escaped)) as RunEvent[];
}

function loadScenario(store: RunStore, player: FixturePlayer, scenario: Scenario) {
  player.clear();
  store.getState().actions.reset();
  store.getState().actions.addUserMessage(scenario.userText);
  player.enqueue(fixture(scenario.start));
}

export default function ChatFixturePage() {
  const { t } = useTranslation("dev");
  const [store] = useState(() => createRunStore());
  const [player] = useState(
    () => new FixturePlayer((event) => store.getState().actions.applyEvent(event)),
  );
  const progress = useSyncExternalStore(player.subscribe, player.getSnapshot);
  const [scenario, setScenario] = useState<Scenario>(SCENARIOS[0]);
  const [speed, setSpeed] = useState("normal");
  const followUps = useRef(0);

  useEffect(() => {
    loadScenario(store, player, SCENARIOS[0]);
    return () => player.clear();
  }, [store, player]);

  function choose(next: Scenario) {
    setScenario(next);
    loadScenario(store, player, next);
  }

  // 서버라면 POST /runs/{rid}/input이 interaction.resolved를 남기고 런을 다시 큐에 넣는다(설계 4.9).
  async function respond(runId: string, input: InteractionInput) {
    const state = store.getState();
    const pending = state.pendingInteraction;
    if (!pending) return;
    await new Promise((resolve) => setTimeout(resolve, 300));
    const resolved: RunEvent = {
      run_id: runId,
      session_id: state.sessionId ?? "",
      seq: (state.lastSeq[runId] ?? 0) + 1,
      ts: new Date().toISOString(),
      type: "interaction.resolved",
      data: {
        interaction_id: pending.interaction_id,
        kind: pending.kind,
        ...("decision" in input
          ? {
              decision: input.decision,
              note: input.decision === "reject" ? (input.note ?? null) : null,
              session_auto_approve:
                input.decision === "approve" ? Boolean(input.session_auto_approve) : false,
            }
          : { answer: input.answer }),
      },
    };
    state.actions.applyEvent(resolved);
    let next: RunEvent[] = [];
    if ("answer" in input && scenario.answer)
      next = withAnswer(fixture(scenario.answer), input.answer);
    else if ("decision" in input && input.decision === "approve" && scenario.approve)
      next = fixture(scenario.approve);
    else if ("decision" in input && input.decision === "reject" && scenario.reject)
      next = fixture(scenario.reject);
    player.enqueue(next);
  }

  function send(text: string) {
    followUps.current += 1;
    store.getState().actions.addUserMessage(text);
    player.enqueue(renamed(fixture("text-only"), `f${followUps.current}`));
  }

  function stop() {
    const state = store.getState();
    player.clear();
    if (!state.runId) return;
    state.actions.applyEvent({
      run_id: state.runId,
      session_id: state.sessionId ?? "",
      seq: (state.lastSeq[state.runId] ?? 0) + 1,
      ts: new Date().toISOString(),
      type: "run.completed",
      data: {
        status: "cancelled",
        usage_total: {
          input_tokens: state.usage.inputTokens,
          output_tokens: state.usage.outputTokens,
        },
        steps: state.step,
      },
    });
  }

  return (
    <div className="flex h-full min-h-0 bg-canvas">
      <aside className="flex w-72 shrink-0 flex-col gap-4 overflow-y-auto border-r border-line bg-surface p-4">
        <div>
          <h1 className="text-base font-semibold text-ink">{t("title")}</h1>
          <p className="mt-1 text-xs text-ink-muted">{t("description")}</p>
        </div>
        <nav aria-label={t("scenario")}>
          <ul className="flex flex-col gap-1">
            {SCENARIOS.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  onClick={() => choose(item)}
                  aria-current={item.id === scenario.id ? "true" : undefined}
                  className={cn(
                    "w-full rounded-md px-2.5 py-1.5 text-left text-sm",
                    item.id === scenario.id
                      ? "bg-accent-soft font-medium text-ink"
                      : "text-ink-muted hover:bg-hover",
                  )}
                >
                  {t(`scenarios.${item.id}`)}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-ink-muted">{t("speed")}</span>
          <Select
            aria-label={t("speed")}
            value={speed}
            onValueChange={(value) => {
              setSpeed(value);
              player.setSpeed(SPEEDS.find((item) => item.key === value)?.value ?? 1);
            }}
            options={SPEEDS.map((item) => ({ value: item.key, label: t(`speeds.${item.key}`) }))}
          />
        </div>
        <Button onClick={() => loadScenario(store, player, scenario)}>{t("restart")}</Button>
        <p className="text-xs text-ink-muted tabular-nums">
          {t("events", { done: progress.done, total: progress.total })}
        </p>
        {progress.total > 0 && progress.done === progress.total ? (
          <p className="text-xs text-ink-subtle">{t("idle")}</p>
        ) : null}
      </aside>
      <main className="min-w-0 flex-1">
        <RunStoreProvider store={store}>
          <ChatPanel
            sessionId="0199d7a0-5c1e-7a00-8000-00000000c0de"
            onSend={send}
            onStop={stop}
            onRespond={respond}
            onDisableAutoApprove={async () => {
              await new Promise((resolve) => setTimeout(resolve, 300));
              store.getState().actions.setSessionAutoApprove(false);
            }}
          />
        </RunStoreProvider>
      </main>
      <aside className="hidden w-80 shrink-0 border-l border-line p-4 xl:block">
        <WorkspacePanel />
      </aside>
    </div>
  );
}
