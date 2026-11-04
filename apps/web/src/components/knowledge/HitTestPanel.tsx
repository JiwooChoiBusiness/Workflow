import { Search } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { KnowledgeBaseOut, RetrievalSettings, RetrieveResponse } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { MetadataFilterBuilder } from "@/components/flow/forms/MetadataFilterBuilder";
import { useCreateEvalCase, useMetadataFields, useRetrieve } from "@/features/knowledge/api";
import { retrievalSummary, warningMessage } from "@/features/knowledge/messages";
import {
  conditionProblems,
  emptyFilterForm,
  filterRequest,
  type FilterMode,
} from "@/features/knowledge/metadataFilter";
import {
  fromRetrievalForm,
  retrievalFormValid,
  toRetrievalForm,
} from "@/features/knowledge/retrievalSettings";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { useKbOptions } from "@/features/workflows/api";
import { AddCaseDialog } from "./AddCaseDialog";
import { QueryLogTable } from "./QueryLogTable";
import { ResultCard } from "./ResultCard";
import { RetrievalSettingsForm } from "./RetrievalSettingsForm";

const MAX_QUERY = 250;
// 검색 시험은 값을 상수로만 쓴다. 자동 필터는 모델 호출이 들어 노드에서만 고른다.
const FILTER_MODES: readonly FilterMode[] = ["disabled", "manual"];
const NO_OPERATORS: Record<string, readonly string[]> = {};

interface Ran {
  query: string;
  settings: RetrievalSettings;
  response: RetrieveResponse;
  receivedAt: number;
}

// 검색 시험(설계 7.7, 12.3): 질의, 이 시험에만 쓰는 설정, 결과 카드, 경고, 질의 기록, 정답 등록(확장).
export function HitTestPanel({
  kb,
  topKMax,
  queryLogDays,
}: {
  kb: KnowledgeBaseOut;
  topKMax: number;
  queryLogDays: number;
}) {
  const { t } = useTranslation("knowledge");
  const errorText = useKnowledgeError();
  const retrieve = useRetrieve(kb.id);
  const createCase = useCreateEvalCase(kb.id);
  const [query, setQuery] = useState("");
  const [custom, setCustom] = useState(false);
  const [form, setForm] = useState(() => toRetrievalForm(kb.retrieval_model));
  const [filter, setFilter] = useState(emptyFilterForm);
  const metadata = useMetadataFields(kb.id);
  const operators = useKbOptions().data?.operators ?? NO_OPERATORS;
  const fields = metadata.data
    ? metadata.data.items.map((field) => ({ name: field.name, type: field.type }))
    : null;
  const [ran, setRan] = useState<Ran | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [marking, setMarking] = useState<string | null>(null);
  // 질의 기록에서 평가셋에 넣을 질문. key가 바뀌면 대화상자가 빈 상태로 다시 열린다.
  const [draft, setDraft] = useState({ question: "", key: 0 });
  const [adding, setAdding] = useState(false);

  const formValid =
    (!custom || retrievalFormValid(form, topKMax)) &&
    Object.keys(conditionProblems(filter, fields, operators)).length === 0;

  function run(text = query) {
    const trimmed = text.trim();
    if (!trimmed || trimmed.length > MAX_QUERY || !formValid) return;
    const settings = custom ? fromRetrievalForm(form) : kb.retrieval_model;
    setProblem(null);
    retrieve.mutate(
      {
        query: trimmed,
        retrieval_model: custom ? settings : null,
        metadata_filter: filterRequest(filter, fields),
      },
      {
        onSuccess: (response) =>
          setRan({ query: trimmed, settings, response, receivedAt: Date.now() }),
        onError: (error) => {
          setRan(null);
          setProblem(errorText(error));
        },
      },
    );
  }

  function mark(target: "segment" | "document", segmentId: string, documentId: string) {
    if (!ran) return;
    setMarking(segmentId);
    createCase.mutate(
      {
        question: ran.query,
        expected:
          target === "segment"
            ? { segment_ids: [segmentId], document_ids: [] }
            : { document_ids: [documentId], segment_ids: [] },
        note: null,
      },
      {
        onSuccess: () => toast.success(t("hitTest.marked")),
        onError: (error) => toast.error(errorText(error)),
        onSettled: () => setMarking(null),
      },
    );
  }

  const records = ran?.response.records ?? [];
  const top = Math.max(...records.map((record) => record.score), 0);
  const smallScores = ran?.settings.reranking_mode === "rrf" || top > 1;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <Panel
          title={t("hitTest.title")}
          description={t("hitTest.description")}
          className="self-start"
        >
          <form
            className="flex flex-col gap-3"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              run();
            }}
          >
            <Field
              label={t("hitTest.query")}
              hint={t("hitTest.queryCount", { count: query.length, max: MAX_QUERY })}
            >
              <Textarea
                rows={3}
                value={query}
                placeholder={t("hitTest.queryPlaceholder")}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    run();
                  }
                }}
              />
            </Field>
            <Field
              label={t("hitTest.custom")}
              hint={
                custom
                  ? undefined
                  : t("hitTest.customHint", { summary: retrievalSummary(kb.retrieval_model, t) })
              }
              inline
            >
              <Switch checked={custom} onCheckedChange={setCustom} />
            </Field>
            {custom ? (
              <RetrievalSettingsForm
                value={form}
                onChange={setForm}
                technique={kb.indexing_technique}
                topKMax={topKMax}
              />
            ) : null}
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-2 text-sm font-medium text-ink">{t("filter.title")}</legend>
              <MetadataFilterBuilder
                value={filter}
                onChange={setFilter}
                fields={fields}
                fieldsError={metadata.error}
                onRetryFields={() => void metadata.refetch()}
                operators={operators}
                modes={FILTER_MODES}
              />
            </fieldset>
            <Button
              type="submit"
              variant="primary"
              loading={retrieve.isPending}
              disabled={!query.trim() || query.length > MAX_QUERY || !formValid}
            >
              <Search className="size-4" aria-hidden />
              {t("hitTest.run")}
            </Button>
          </form>
        </Panel>
        <section aria-label={t("hitTest.title")} className="flex min-w-0 flex-col gap-3">
          {problem ? <Alert tone="danger" title={problem} /> : null}
          {ran ? (
            <>
              {ran.response.warnings?.map((warning) => (
                <Alert
                  key={warning.code}
                  tone="warn"
                  title={warningMessage(warning, t, ran.receivedAt)}
                />
              ))}
              <p className="text-sm text-ink-muted">
                {t("hitTest.results", {
                  count: records.length,
                  ms: ran.response.latency_ms ?? 0,
                })}
              </p>
              {records.length === 0 ? (
                <EmptyState icon={Search} title={t("hitTest.noResults")} />
              ) : (
                records.map((record, index) => (
                  <ResultCard
                    key={record.segment.id}
                    kbId={kb.id}
                    record={record}
                    position={index + 1}
                    settings={ran.settings}
                    technique={kb.indexing_technique}
                    warnings={ran.response.warnings ?? []}
                    barScale={smallScores ? top : 1}
                    marking={marking === record.segment.id}
                    onMark={(target) => mark(target, record.segment.id, record.segment.document_id)}
                  />
                ))
              )}
            </>
          ) : problem ? null : (
            <EmptyState icon={Search} title={t("hitTest.idle")} />
          )}
        </section>
      </div>
      <Panel
        title={t("hitTest.logs")}
        description={
          queryLogDays > 0 ? t("hitTest.logsHint", { days: queryLogDays }) : t("hitTest.logsOff")
        }
      >
        <QueryLogTable
          kbId={kb.id}
          onReuse={(text) => {
            setQuery(text);
            run(text);
          }}
          onAddCase={(text) => {
            setDraft((current) => ({ question: text, key: current.key + 1 }));
            setAdding(true);
          }}
        />
      </Panel>
      <AddCaseDialog
        key={draft.key}
        kbId={kb.id}
        open={adding}
        onOpenChange={setAdding}
        initialQuestion={draft.question}
      />
    </div>
  );
}
