import { FileUp, X } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import type {
  DocumentPreview,
  KbDocumentsCreated,
  KnowledgeBaseOut,
  KnowledgeDefaults,
} from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Checkbox, Switch } from "@/components/ui/toggle";
import {
  useBatchStatus,
  usePreviewDocument,
  useUploadDocuments,
  type UploadOptions,
} from "@/features/knowledge/api";
import { isInProgress, statusTone } from "@/features/knowledge/documentStatus";
import { completeRule, ruleErrors, toProcessRule } from "@/features/knowledge/processRule";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { formatBytes } from "@/lib/format";
import { ChunkPreview, type DailyBudget } from "./ChunkPreview";
import { IndexUnavailableNotice } from "./IndexUnavailableNotice";
import { ProcessRuleForm } from "./ProcessRuleForm";

const EXTENSIONS = [".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".pdf", ".docx"];

type Mode = "files" | "text";

function accepted(file: File): boolean {
  const name = file.name.toLowerCase();
  return EXTENSIONS.some((extension) => name.endsWith(extension));
}

function isPdf(file: File): boolean {
  return file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf";
}

// 올린 묶음의 진행(콘솔 indexing-status). 닫아도 색인은 계속된다.
function BatchProgress({ kbId, created }: { kbId: string; created: KbDocumentsCreated }) {
  const { t } = useTranslation("knowledge");
  const status = useBatchStatus(kbId, created.batch);
  const byId = new Map((status.data?.data ?? []).map((item) => [item.id, item]));
  return (
    <section aria-label={t("upload.result")} className="flex flex-col gap-2">
      <p className="text-sm font-medium text-ink">{t("upload.result")}</p>
      <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
        {created.documents.map((document) => {
          const live = byId.get(document.id);
          const indexingStatus = live?.indexing_status ?? document.indexing_status;
          const total = live?.total_segments ?? 0;
          const done = live?.completed_segments ?? 0;
          return (
            <li key={document.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className="min-w-0 flex-1 truncate text-ink">{document.name}</span>
              {indexingStatus === "indexing" && total > 0 ? (
                <span className="text-xs text-ink-muted tabular-nums">
                  {t("documents.chunksValue", { done, total })}
                </span>
              ) : null}
              <Badge
                tone={
                  indexingStatus === "completed"
                    ? "ok"
                    : indexingStatus === "error"
                      ? "danger"
                      : isInProgress({ indexing_status: indexingStatus })
                        ? "info"
                        : statusTone("paused")
                }
              >
                {t(`stage.${indexingStatus}`)}
              </Badge>
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-ink-muted">{t("upload.resultHint")}</p>
    </section>
  );
}

// 문서 올리기(설계 6.2, 6.4, 12.3): 파일 끌어 놓기나 글, 이 문서만의 규칙, 미리보기 청크와 예상 소요.
export function UploadDialog({
  kb,
  defaults,
  open,
  onOpenChange,
}: {
  kb: KnowledgeBaseOut;
  defaults: KnowledgeDefaults | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const maxFiles = defaults?.upload_max_files ?? 20;
  const maxMb = defaults?.upload_max_mb ?? 50;
  const errorText = useKnowledgeError({ uploadMaxMb: maxMb, uploadMaxFiles: maxFiles });
  const upload = useUploadDocuments(kb.id);
  const previewMutation = usePreviewDocument(kb.id);
  const fileInput = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<Mode>("files");
  const [files, setFiles] = useState<File[]>([]);
  const [rejected, setRejected] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [override, setOverride] = useState(false);
  const [rule, setRule] = useState(() => completeRule(kb.process_rule));
  const [allowDuplicate, setAllowDuplicate] = useState(false);
  const [modelRead, setModelRead] = useState(false);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [created, setCreated] = useState<KbDocumentsCreated | null>(null);
  const [dragging, setDragging] = useState(false);

  const qa = kb.doc_form === "qa_model";
  const errors = override ? ruleErrors(rule, kb.doc_form) : {};
  const ruleInvalid = Object.keys(errors).length > 0;
  const processRule = override && !qa ? toProcessRule(rule) : undefined;
  const tier = kb.data_policy.key_tier;
  const limits = defaults?.limiter[tier];
  const rate = limits ? Math.max(1, limits.rpm - limits.reserve) : 80;
  const usage = defaults?.daily.find((item) => item.tier === tier);
  const daily: DailyBudget | null = usage
    ? { remaining: usage.bulk_remaining ?? null, blockedUntil: usage.blocked_until ?? null }
    : null;
  const hasPdf = files.some(isPdf);
  // index.db를 쓸 수 없으면 올려도 색인되지 않으므로 올리기를 막는다. 미리보기는 색인과 무관해 둔다.
  const indexDown = defaults ? !defaults.index_available : false;

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setMode("files");
      setFiles([]);
      setRejected([]);
      setName("");
      setText("");
      setOverride(false);
      setRule(completeRule(kb.process_rule));
      setAllowDuplicate(false);
      setModelRead(false);
      setPreview(null);
      setProblem(null);
      setCreated(null);
    }
  }

  function addFiles(list: FileList | File[]) {
    const notes: string[] = [];
    const next = [...files];
    for (const file of Array.from(list)) {
      if (!accepted(file)) {
        notes.push(t("upload.badType", { name: file.name }));
      } else if (file.size > maxMb * 1024 * 1024) {
        notes.push(t("upload.tooLarge", { name: file.name, mb: maxMb }));
      } else if (next.length >= maxFiles) {
        notes.push(t("upload.tooMany", { count: maxFiles }));
        break;
      } else if (!next.some((item) => item.name === file.name && item.size === file.size)) {
        next.push(file);
      }
    }
    setFiles(next);
    setRejected(notes);
    setPreview(null);
    setProblem(null);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (event.dataTransfer.files.length > 0) addFiles(event.dataTransfer.files);
  }

  function options(): UploadOptions {
    return {
      process_rule: processRule,
      allow_duplicate: allowDuplicate,
      extract_mode: modelRead && hasPdf ? "model" : undefined,
    };
  }

  function runPreview() {
    setProblem(null);
    const input =
      mode === "files"
        ? files[0]
          ? { kind: "file" as const, file: files[0], processRule }
          : null
        : text.trim()
          ? { kind: "text" as const, text, processRule }
          : null;
    if (!input) {
      setProblem(t("upload.nothing"));
      return;
    }
    previewMutation.mutate(input, {
      onSuccess: setPreview,
      onError: (error) => setProblem(errorText(error)),
    });
  }

  function submit() {
    setProblem(null);
    if (mode === "files" ? files.length === 0 : !text.trim() || !name.trim()) {
      setProblem(mode === "text" && text.trim() ? tc("validation.required") : t("upload.nothing"));
      return;
    }
    const input =
      mode === "files"
        ? { kind: "files" as const, files, options: options() }
        : { kind: "text" as const, name: name.trim(), text, options: options() };
    upload.mutate(input, {
      onSuccess: (result) => setCreated(result),
      onError: (error) => setProblem(errorText(error)),
    });
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent title={t("upload.title")} className="w-[min(46rem,calc(100vw-2rem))]">
        {created ? (
          <div className="flex flex-col gap-4">
            <Alert tone="info" title={t("upload.uploaded", { count: created.documents.length })} />
            <BatchProgress kbId={kb.id} created={created} />
            <DialogFooter>
              <Button variant="primary" onClick={() => close(false)}>
                {tc("actions.close")}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {indexDown ? <IndexUnavailableNotice reason={defaults?.index_error} /> : null}
            <Tabs
              value={mode}
              onValueChange={(value) => {
                setMode(value as Mode);
                setPreview(null);
                setProblem(null);
              }}
            >
              <TabsList aria-label={t("upload.modeLabel")}>
                <TabsTrigger value="files">{t("upload.files")}</TabsTrigger>
                <TabsTrigger value="text">{t("upload.text")}</TabsTrigger>
              </TabsList>
              <TabsContent value="files" className="flex flex-col gap-2 pt-4">
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  accept={EXTENSIONS.join(",")}
                  className="hidden"
                  aria-hidden
                  tabIndex={-1}
                  onChange={(event) => {
                    if (event.target.files) addFiles(event.target.files);
                    event.target.value = "";
                  }}
                />
                <div
                  className={
                    "flex min-h-24 flex-col items-center justify-center gap-2 rounded-md border border-dashed px-3 py-4 text-center " +
                    (dragging ? "border-accent bg-accent-soft" : "border-line")
                  }
                  onDragOver={(event) => {
                    event.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={onDrop}
                >
                  <Button size="sm" onClick={() => fileInput.current?.click()}>
                    <FileUp className="size-3.5" aria-hidden />
                    {t("upload.drop")}
                  </Button>
                  <p className="text-xs text-ink-muted">
                    {t("upload.dropHint", { count: maxFiles, mb: maxMb })}
                  </p>
                </div>
                {rejected.map((note) => (
                  <p key={note} className="text-xs text-warn">
                    {note}
                  </p>
                ))}
                {files.length > 0 ? (
                  <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
                    {files.map((file) => (
                      <li
                        key={`${file.name}-${file.size}`}
                        className="flex items-center gap-3 px-3 py-1.5"
                      >
                        <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">
                          {file.name}
                        </span>
                        <span className="shrink-0 text-xs text-ink-muted">
                          {formatBytes(file.size)}
                        </span>
                        <Button
                          size="iconSm"
                          variant="ghost"
                          aria-label={t("upload.remove", { name: file.name })}
                          title={t("upload.remove", { name: file.name })}
                          onClick={() => {
                            setFiles(files.filter((item) => item !== file));
                            setPreview(null);
                          }}
                        >
                          <X className="size-4" aria-hidden />
                        </Button>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {files.length > 1 ? (
                  <p className="text-xs text-ink-muted">
                    {t("upload.previewOf", { name: files[0].name })}
                  </p>
                ) : null}
              </TabsContent>
              <TabsContent value="text" className="flex flex-col gap-3 pt-4">
                <Field label={t("upload.name")} required>
                  <Input value={name} onChange={(event) => setName(event.target.value)} />
                </Field>
                <Field label={t("upload.body")} required>
                  <Textarea
                    rows={8}
                    value={text}
                    onChange={(event) => {
                      setText(event.target.value);
                      setPreview(null);
                    }}
                  />
                </Field>
              </TabsContent>
            </Tabs>
            {qa ? (
              <p className="text-xs text-ink-muted">{t("upload.qaNoRule")}</p>
            ) : (
              <div className="flex flex-col gap-3 rounded-md border border-line px-3 py-3">
                <Field label={t("upload.ruleOverride")} hint={t("upload.ruleHint")} inline>
                  <Switch
                    checked={override}
                    onCheckedChange={(checked) => {
                      setOverride(checked);
                      setPreview(null);
                    }}
                  />
                </Field>
                {override ? (
                  <ProcessRuleForm
                    value={rule}
                    docForm={kb.doc_form}
                    errors={errors}
                    onChange={(next) => {
                      setRule(next);
                      setPreview(null);
                    }}
                  />
                ) : null}
              </div>
            )}
            <div className="flex flex-col gap-2">
              <Field label={t("upload.allowDuplicate")} inline>
                <Checkbox
                  checked={allowDuplicate}
                  onCheckedChange={(checked) => setAllowDuplicate(checked === true)}
                />
              </Field>
              {mode === "files" && hasPdf ? (
                <Field label={t("upload.modelRead")} hint={t("upload.modelReadHint")} inline>
                  <Checkbox
                    checked={modelRead}
                    onCheckedChange={(checked) => setModelRead(checked === true)}
                  />
                </Field>
              ) : null}
            </div>
            {preview ? (
              <ChunkPreview
                preview={preview}
                docForm={kb.doc_form}
                technique={kb.indexing_technique}
                rate={rate}
                daily={daily}
              />
            ) : null}
            {problem ? <Alert tone="danger" title={problem} /> : null}
            <DialogFooter className="flex-wrap">
              <Button variant="secondary" onClick={() => close(false)}>
                {tc("actions.cancel")}
              </Button>
              <Button
                loading={previewMutation.isPending}
                disabled={ruleInvalid || upload.isPending}
                onClick={runPreview}
              >
                {t("upload.preview")}
              </Button>
              <Button
                variant="primary"
                loading={upload.isPending}
                disabled={ruleInvalid || indexDown}
                onClick={submit}
              >
                {t("upload.submit")}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
