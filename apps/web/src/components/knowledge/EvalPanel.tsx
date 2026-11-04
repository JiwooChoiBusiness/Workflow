import { FileUp, ListChecks, Play, Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import type {
  EvalImportResult,
  EvalRunOut,
  KnowledgeBaseOut,
  RetrievalSettings,
} from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { RadioCards } from "@/components/ui/radio-cards";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Checkbox, Switch } from "@/components/ui/toggle";
import {
  useDeleteEvalCase,
  useEvalCases,
  useEvalRuns,
  useImportEvalCases,
  useRunEval,
} from "@/features/knowledge/api";
import { formatMetric, METRIC_KEYS, rankBuckets } from "@/features/knowledge/evalMetrics";
import { retrievalSummary, warningMessage } from "@/features/knowledge/messages";
import {
  fromRetrievalForm,
  retrievalFormValid,
  toRetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatDateTime } from "@/lib/format";
import { AddCaseDialog } from "./AddCaseDialog";
import { EvalRunCompare } from "./EvalRunCompare";
import { KbErrorAlert } from "./KbErrorAlert";
import { RetrievalSettingsForm } from "./RetrievalSettingsForm";

function problemsOf(error: unknown): { line: number; message: string }[] {
  if (!isApiError(error) || error.code !== "eval_import_empty") return [];
  const details = error.details as { problems?: unknown } | null;
  return Array.isArray(details?.problems)
    ? (details.problems as { line: number; message: string }[])
    : [];
}

function ImportDialog({
  kbId,
  open,
  onOpenChange,
}: {
  kbId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const importCases = useImportEvalCases(kbId);
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [csv, setCsv] = useState("");
  const [replace, setReplace] = useState(false);
  const [result, setResult] = useState<EvalImportResult | null>(null);
  const [problem, setProblem] = useState<{
    title: string;
    lines: { line: number; message: string }[];
  } | null>(null);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setFile(null);
      setCsv("");
      setReplace(false);
      setResult(null);
      setProblem(null);
    }
  }

  const lines = result?.problems ?? problem?.lines ?? [];

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        title={t("eval.importTitle")}
        description={t("eval.importHint")}
        className="w-[min(40rem,calc(100vw-2rem))]"
      >
        <div className="flex flex-col gap-4">
          <input
            ref={fileInput}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            aria-hidden
            tabIndex={-1}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              event.target.value = "";
            }}
          />
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={() => fileInput.current?.click()}>
              <FileUp className="size-3.5" aria-hidden />
              {t("eval.importFile")}
            </Button>
            {file ? <span className="truncate font-mono text-sm">{file.name}</span> : null}
          </div>
          <Field label={t("eval.importText")}>
            <Textarea
              rows={6}
              className="font-mono text-xs"
              value={csv}
              disabled={file !== null}
              placeholder={
                "question,expected_document_name,note\n연차는 며칠 이월되나요?,인사 규정,"
              }
              onChange={(event) => setCsv(event.target.value)}
            />
          </Field>
          <Field label={t("eval.replace")} hint={t("eval.replaceHint")} inline>
            <Switch checked={replace} onCheckedChange={setReplace} />
          </Field>
          {result ? (
            <Alert
              tone="info"
              title={t("eval.imported", { created: result.created, total: result.total })}
            />
          ) : null}
          {problem ? <Alert tone="danger" title={problem.title} /> : null}
          {lines.length > 0 ? (
            <div>
              <p className="mb-1 text-xs font-medium text-ink-muted">{t("eval.problems")}</p>
              <ul className="max-h-40 overflow-y-auto text-xs text-ink">
                {lines.map((item) => (
                  <li key={`${item.line}-${item.message}`}>
                    {t("eval.problem", { line: item.line, message: item.message })}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => close(false)}>
              {tc("actions.close")}
            </Button>
            <Button
              variant="primary"
              loading={importCases.isPending}
              disabled={!file && !csv.trim()}
              onClick={() => {
                setResult(null);
                setProblem(null);
                importCases.mutate(
                  { file: file ?? undefined, csv, replace },
                  {
                    onSuccess: setResult,
                    onError: (error) =>
                      setProblem({ title: errorText(error), lines: problemsOf(error) }),
                  },
                );
              }}
            >
              {t("eval.importSubmit")}
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// names는 평가셋이 가리키는 살아 있는 정답 문서뿐이다. 상위 결과에서 그 밖의 문서는 이름 없이 '다른
// 문서'로 보인다.
function RunResult({ run, names }: { run: EvalRunOut; names: Record<string, string> }) {
  const { t } = useTranslation("knowledge");
  const buckets = rankBuckets(run.cases);
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {METRIC_KEYS.map((key) => (
          <div key={key} className="rounded-md border border-line px-3 py-2">
            <p className="text-xs text-ink-muted">{t(`eval.metrics.${key}`)}</p>
            <p className="font-mono text-lg font-semibold text-ink tabular-nums">
              {formatMetric(run.metrics[key])}
            </p>
          </div>
        ))}
      </div>
      <p className="text-xs text-ink-muted">
        {t("eval.duration", {
          cases: run.metrics.cases,
          seconds: (run.duration_ms / 1000).toFixed(1),
        })}{" "}
        · {t("eval.buckets", { ...buckets })}
      </p>
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium">{t("eval.question")}</th>
            <th className="pb-2 text-right font-medium">{t("eval.rank")}</th>
            <th className="pb-2 pl-3 font-medium">{t("eval.top")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {run.cases.map((item) => (
            <tr key={item.case_id}>
              <td className="max-w-80 truncate py-1.5 pr-3" title={item.question}>
                {item.question}
              </td>
              <td className="py-1.5 text-right">
                {item.rank === null || item.rank === undefined ? (
                  <Badge tone="danger">{t("eval.missed")}</Badge>
                ) : (
                  <Badge tone={item.rank === 1 ? "ok" : "neutral"}>{item.rank}</Badge>
                )}
              </td>
              <td className="max-w-72 truncate py-1.5 pl-3 text-xs text-ink-muted">
                {(item.top ?? [])
                  .slice(0, 3)
                  .map((hit) => names[hit.document_id] ?? t("eval.otherDocument"))
                  .join(" · ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 평가 탭(설계 7.7, 12.3): 평가셋(직접 넣기·CSV 가져오기), 실행과 결과 표, 저장한 실행 비교(확장).
export function EvalPanel({ kb, topKMax }: { kb: KnowledgeBaseOut; topKMax: number }) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const errorText = useKnowledgeError();
  const cases = useEvalCases(kb.id);
  const runs = useEvalRuns(kb.id);
  const run = useRunEval(kb.id);
  const remove = useDeleteEvalCase(kb.id);
  // 정답 문서 이름은 평가셋 응답에 함께 온다. 여기 없는 id는 지운 문서다.
  const names = cases.data?.document_names ?? {};
  const [adding, setAdding] = useState(false);
  const [addKey, setAddKey] = useState(0);
  const [importing, setImporting] = useState(false);
  const [mode, setMode] = useState<"kb" | "custom">("kb");
  const [form, setForm] = useState(() => toRetrievalForm(kb.retrieval_model));
  const [save, setSave] = useState(true);
  const [last, setLast] = useState<EvalRunOut | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [comparing, setComparing] = useState<[string, string] | null>(null);
  const formValid = mode === "kb" || retrievalFormValid(form, topKMax);

  function start() {
    const settings: RetrievalSettings | null = mode === "custom" ? fromRetrievalForm(form) : null;
    setRunError(null);
    run.mutate(
      { retrieval_model: settings, save },
      {
        onSuccess: setLast,
        onError: (error) => setRunError(errorText(error)),
      },
    );
  }

  let caseList;
  if (cases.isPending) {
    caseList = <PageSpinner label={tc("state.loading")} />;
  } else if (cases.isError) {
    caseList = (
      <KbErrorAlert
        error={cases.error}
        title={t("eval.loadFailed")}
        onRetry={() => void cases.refetch()}
      />
    );
  } else if (cases.data.items.length === 0) {
    caseList = <EmptyState icon={ListChecks} title={t("eval.empty")} />;
  } else {
    caseList = (
      <div className="max-h-80 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-surface text-left text-xs text-ink-muted">
            <tr>
              <th className="pb-2 font-medium">{t("eval.question")}</th>
              <th className="pb-2 font-medium">{t("eval.expected")}</th>
              <th className="pb-2 font-medium">{t("eval.note")}</th>
              <th className="pb-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {cases.data.items.map((item) => {
              const documents = item.expected.document_ids ?? [];
              const segments = item.expected.segment_ids ?? [];
              return (
                <tr key={item.id}>
                  <td className="max-w-80 truncate py-1.5 pr-3" title={item.question}>
                    {item.question}
                  </td>
                  <td className="max-w-56 truncate py-1.5 pr-3 text-xs text-ink-muted">
                    {documents.length > 0
                      ? t("eval.expectedDocuments", {
                          names: documents
                            .map((id) => names[id] ?? t("eval.unknownDocument"))
                            .join(", "),
                        })
                      : t("eval.expectedSegments", { count: segments.length })}
                  </td>
                  <td className="max-w-40 truncate py-1.5 pr-3 text-xs text-ink-muted">
                    {item.note ?? ""}
                  </td>
                  <td className="py-1 text-right">
                    <Button
                      size="iconSm"
                      variant="dangerGhost"
                      aria-label={t("eval.deleteCase", { question: item.question })}
                      title={t("eval.deleteCase", { question: item.question })}
                      onClick={() =>
                        remove.mutate(item.id, {
                          onError: (error) => toast.error(errorText(error)),
                        })
                      }
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  const savedRuns = runs.data ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title={t("eval.cases")}
        description={t("eval.casesHint")}
        actions={
          <>
            {cases.data ? (
              <span className="text-xs text-ink-muted">
                {t("eval.casesCount", { count: cases.data.total })}
              </span>
            ) : null}
            <Button size="sm" onClick={() => setImporting(true)}>
              <FileUp className="size-3.5" aria-hidden />
              {t("eval.import")}
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setAddKey((key) => key + 1);
                setAdding(true);
              }}
            >
              <Plus className="size-3.5" aria-hidden />
              {t("eval.add")}
            </Button>
          </>
        }
      >
        {caseList}
      </Panel>
      <Panel title={t("eval.run")} description={t("eval.runHint")}>
        <div className="flex flex-col gap-4">
          <RadioCards
            ariaLabel={t("eval.run")}
            value={mode}
            onValueChange={(value) => setMode(value as "kb" | "custom")}
            className="sm:grid sm:grid-cols-2"
            options={[
              {
                value: "kb",
                label: t("eval.useKbSettings", {
                  summary: retrievalSummary(kb.retrieval_model, t),
                }),
              },
              { value: "custom", label: t("eval.useCustom") },
            ]}
          />
          {mode === "custom" ? (
            <RetrievalSettingsForm
              value={form}
              onChange={setForm}
              technique={kb.indexing_technique}
              topKMax={topKMax}
            />
          ) : null}
          <div className="flex flex-wrap items-center gap-4">
            <Field label={t("eval.save")} inline>
              <Switch checked={save} onCheckedChange={setSave} />
            </Field>
            <Button
              variant="primary"
              className="ml-auto"
              loading={run.isPending}
              disabled={!formValid || (cases.data?.total ?? 0) === 0}
              onClick={start}
            >
              <Play className="size-4" aria-hidden />
              {run.isPending ? t("eval.running") : t("eval.run")}
            </Button>
          </div>
          {runError ? <Alert tone="danger" title={runError} /> : null}
          {last ? (
            <section aria-label={t("eval.result")} className="flex flex-col gap-2">
              {(last.warnings ?? []).length > 0 ? (
                <Alert
                  tone="warn"
                  title={(last.warnings ?? [])
                    .map((warning) => warningMessage(warning, t))
                    .join(" ")}
                />
              ) : null}
              <RunResult run={last} names={names} />
            </section>
          ) : null}
        </div>
      </Panel>
      <Panel
        title={t("eval.runs")}
        description={t("eval.runsHint")}
        actions={
          <Button
            size="sm"
            disabled={picked.length !== 2}
            title={picked.length !== 2 ? t("eval.compareNeedTwo") : undefined}
            onClick={() => setComparing([picked[0], picked[1]])}
          >
            {t("eval.compare")}
          </Button>
        }
      >
        {runs.isPending ? (
          <PageSpinner label={tc("state.loading")} />
        ) : savedRuns.length === 0 ? (
          <p className="text-sm text-ink-muted">{t("eval.runsEmpty")}</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-ink-muted">
              <tr>
                <th className="w-8 pb-2" />
                <th className="pb-2 font-medium">{t("document.created")}</th>
                <th className="pb-2 font-medium">{t("retrieval.method")}</th>
                {METRIC_KEYS.map((key) => (
                  <th key={key} className="pb-2 text-right font-medium">
                    {t(`eval.metrics.${key}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {savedRuns.map((item) => {
                const checked = picked.includes(item.id);
                return (
                  <tr key={item.id}>
                    <td className="py-1.5">
                      <Checkbox
                        checked={checked}
                        aria-label={t("eval.compareSelect", {
                          when: formatDateTime(item.created_at),
                        })}
                        onCheckedChange={(on) =>
                          setPicked((current) =>
                            on === true
                              ? [...current.filter((id) => id !== item.id), item.id].slice(-2)
                              : current.filter((id) => id !== item.id),
                          )
                        }
                      />
                    </td>
                    <td className="py-1.5 pr-3 text-xs whitespace-nowrap">
                      {formatDateTime(item.created_at)}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-ink-muted">
                      {retrievalSummary(item.settings, t)}
                    </td>
                    {METRIC_KEYS.map((key) => (
                      <td key={key} className="py-1.5 text-right font-mono tabular-nums">
                        {formatMetric(item.metrics[key])}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {comparing ? <EvalRunCompare kbId={kb.id} runIds={comparing} /> : null}
      <AddCaseDialog key={addKey} kbId={kb.id} open={adding} onOpenChange={setAdding} />
      <ImportDialog kbId={kb.id} open={importing} onOpenChange={setImporting} />
    </div>
  );
}
