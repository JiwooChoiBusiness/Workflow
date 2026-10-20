import { CircleAlert, FileUp, TriangleAlert, X } from "lucide-react";
import { useId, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type { WfIssue, WorkflowKind } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import {
  dslErrorText,
  localDslProblem,
  summarizeImport,
  useImportWorkflowDsl,
  type DslErrorText,
  type DslInput,
  type WorkflowImportOut,
} from "@/features/workflows/dsl";
import "@/features/workflows/i18n";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { formatBytes } from "@/lib/format";

// 편집기에서 연 경우. 새로 만들지 않고 이 워크플로우의 초안을 덮어쓴다(?into=).
export interface ImportTarget {
  workflowId: string;
  kind: WorkflowKind;
  // 덮어쓰기 직전. 편집기는 기다리던 자동 저장을 먼저 끝내 덮어쓴 초안과 부딪히지 않게 한다.
  beforeImport?: () => Promise<unknown>;
  // 덮어쓴 뒤 서버의 초안을 다시 불러온다.
  afterImport: () => Promise<void>;
}

type Source = "file" | "text";

const LIST = "flex max-h-40 flex-col gap-1 overflow-y-auto rounded-md bg-sunken px-3 py-2 text-xs";

function ResultSection({
  title,
  meta,
  hint,
  children,
}: {
  title: string;
  meta?: string;
  hint?: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="flex flex-col gap-1.5">
      <h3 id={id} className="text-sm font-semibold text-ink">
        {title}
        {meta ? <span className="ml-2 font-normal text-ink-muted">{meta}</span> : null}
      </h3>
      {hint ? <p className="text-xs text-ink-muted">{hint}</p> : null}
      {children}
    </section>
  );
}

function Where({ nodeId, field }: { nodeId?: string | null; field?: string | null }) {
  const parts = [nodeId, field].filter(Boolean);
  if (parts.length === 0) return null;
  return <span className="ml-1 font-mono text-ink-muted">({parts.join(" · ")})</span>;
}

function IssueRow({ issue }: { issue: WfIssue }) {
  const { t } = useTranslation("workflows");
  const error = issue.level === "error";
  const Icon = error ? CircleAlert : TriangleAlert;
  return (
    <li className="flex items-start gap-1.5">
      <Icon
        className={cn("mt-0.5 size-3.5 shrink-0", error ? "text-danger" : "text-warn")}
        aria-label={error ? t("issues.error") : t("issues.warning")}
      />
      <span className="min-w-0">
        {issue.message}
        <Where nodeId={issue.node_id} field={issue.field} />
      </span>
    </li>
  );
}

function ImportResult({
  result,
  into,
  onClose,
}: {
  result: WorkflowImportOut;
  into: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const navigate = useNavigate();
  const summary = summarizeImport(result);
  const issues = [...summary.errors, ...summary.issueWarnings];
  const blocked = summary.errors.length > 0 || summary.unsupported.length > 0;
  return (
    <div className="flex flex-col gap-4">
      <Alert
        tone={blocked ? "warn" : "info"}
        title={into ? t("dsl.import.doneInto") : t("dsl.import.done")}
      >
        <span className="mt-1 flex flex-wrap gap-1.5">
          <Badge tone={summary.source === "dify" ? "info" : "neutral"}>
            {t(`dsl.result.source.${summary.source}`)}
          </Badge>
          <Badge>{t("dsl.result.nodes", { count: summary.nodes })}</Badge>
        </span>
      </Alert>
      {summary.unsupported.length > 0 ? (
        <ResultSection
          title={t("dsl.result.unsupported", { count: summary.unsupported.length })}
          hint={t("dsl.result.unsupportedHint")}
        >
          <ul className={LIST}>
            {summary.unsupported.map((item) => (
              <li key={item.nodeId}>
                <span className="font-mono text-ink">{item.nodeId}</span>
                {item.reason ? <span className="ml-1.5 text-ink-muted">{item.reason}</span> : null}
              </li>
            ))}
          </ul>
        </ResultSection>
      ) : null}
      {summary.warnings.length > 0 ? (
        <ResultSection title={t("dsl.result.warnings", { count: summary.warnings.length })}>
          <ul className={LIST}>
            {summary.warnings.map((warning, index) => (
              <li key={`${warning.code}:${warning.node_id ?? ""}:${index}`}>
                {warning.message}
                <Where nodeId={warning.node_id} />
              </li>
            ))}
          </ul>
        </ResultSection>
      ) : null}
      <ResultSection
        title={t("dsl.result.issues")}
        meta={t("dsl.result.issueCounts", {
          errors: summary.errors.length,
          warnings: summary.issueWarnings.length,
        })}
      >
        {issues.length > 0 ? (
          <ul className={LIST}>
            {issues.map((issue, index) => (
              <IssueRow key={`${issue.code}:${issue.node_id ?? ""}:${index}`} issue={issue} />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-ink-muted">{t("dsl.result.noIssues")}</p>
        )}
      </ResultSection>
      <DialogFooter className="mt-1">
        {into ? (
          <Button variant="primary" onClick={onClose}>
            {tc("actions.close")}
          </Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {tc("actions.close")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                onClose();
                navigate(`/workflows/${result.workflow_id}`);
              }}
            >
              {t("dsl.import.openEditor")}
            </Button>
          </>
        )}
      </DialogFooter>
    </div>
  );
}

function ImportForm({ target, onClose }: { target?: ImportTarget; onClose: () => void }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const importDsl = useImportWorkflowDsl();
  const fileInput = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState<Source>("file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<DslErrorText | null>(null);
  const [confirming, setConfirming] = useState(false);
  // 덮어쓰기는 자동 저장 마무리 → 가져오기 → 초안 다시 불러오기까지 한 번에 기다린다.
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<WorkflowImportOut | null>(null);

  if (result) {
    return <ImportResult result={result} into={target !== undefined} onClose={onClose} />;
  }

  const input: DslInput | null =
    source === "text" ? { kind: "text", text } : file ? { kind: "file", file } : null;

  async function run(value: DslInput) {
    setConfirming(false);
    setBusy(true);
    try {
      await target?.beforeImport?.();
      const imported = await importDsl.mutateAsync({ input: value, into: target?.workflowId });
      if (target) {
        try {
          await target.afterImport();
        } catch (failure) {
          toast.error(t("dsl.import.reloadFailed", { message: errorMessage(failure, tc) }));
        }
      }
      setResult(imported);
    } catch (failure) {
      setProblem(dslErrorText(failure, t, tc));
    } finally {
      setBusy(false);
    }
  }

  function submit() {
    const local = localDslProblem(input);
    if (local !== null || input === null) {
      setProblem({ title: t(`dsl.errors.${local ?? "empty"}`), detail: null });
      return;
    }
    setProblem(null);
    if (target) setConfirming(true);
    else void run(input);
  }

  return (
    <div className="flex flex-col gap-4">
      <Tabs
        value={source}
        onValueChange={(value) => {
          setSource(value as Source);
          setProblem(null);
        }}
      >
        <TabsList aria-label={t("dsl.import.source")}>
          <TabsTrigger value="file">{t("dsl.import.fileTab")}</TabsTrigger>
          <TabsTrigger value="text">{t("dsl.import.textTab")}</TabsTrigger>
        </TabsList>
        <TabsContent value="file" className="pt-4">
          <input
            ref={fileInput}
            type="file"
            accept=".yaml,.yml"
            className="hidden"
            aria-hidden
            tabIndex={-1}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setProblem(null);
              event.target.value = "";
            }}
          />
          <div className="flex min-h-14 items-center gap-3 rounded-md border border-dashed border-line px-3 py-2.5">
            <Button size="sm" onClick={() => fileInput.current?.click()}>
              <FileUp className="size-3.5" aria-hidden />
              {t("dsl.import.pick")}
            </Button>
            {file ? (
              <>
                <span className="min-w-0 flex-1 truncate font-mono text-sm text-ink">
                  {file.name}
                </span>
                <span className="shrink-0 text-xs text-ink-muted">{formatBytes(file.size)}</span>
                <Button
                  size="iconSm"
                  variant="ghost"
                  aria-label={t("dsl.import.removeFile", { name: file.name })}
                  title={t("dsl.import.removeFile", { name: file.name })}
                  onClick={() => setFile(null)}
                >
                  <X className="size-4" aria-hidden />
                </Button>
              </>
            ) : (
              <span className="text-sm text-ink-muted">{t("dsl.import.pickHint")}</span>
            )}
          </div>
        </TabsContent>
        <TabsContent value="text" className="pt-4">
          <Textarea
            aria-label={t("dsl.import.yaml")}
            rows={12}
            spellCheck={false}
            className="font-mono text-xs"
            placeholder={"dsl_version: 1\nkind: workflow\nname: …"}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setProblem(null);
            }}
          />
        </TabsContent>
      </Tabs>
      {problem ? (
        <Alert tone="danger" title={problem.title}>
          {problem.detail ? (
            <span className="block font-mono text-xs whitespace-pre-line">{problem.detail}</span>
          ) : null}
        </Alert>
      ) : null}
      <DialogFooter className="mt-1">
        <Button variant="secondary" onClick={onClose}>
          {tc("actions.cancel")}
        </Button>
        <Button variant="primary" loading={busy} onClick={submit}>
          {target ? t("dsl.import.submitInto") : t("dsl.import.submit")}
        </Button>
      </DialogFooter>
      {target ? (
        <ConfirmDialog
          open={confirming}
          onOpenChange={setConfirming}
          title={t("dsl.import.confirmTitle")}
          description={t("dsl.import.confirmBody")}
          confirmLabel={t("dsl.import.confirm")}
          onConfirm={() => {
            if (input) void run(input);
          }}
        />
      ) : null}
    </div>
  );
}

// DSL 가져오기(설계 5장). 목록에서는 새 워크플로우를 만들고, 편집기에서는 target의 초안을 덮어쓴다.
export function ImportDialog({
  open,
  onOpenChange,
  target,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  target?: ImportTarget;
}) {
  const { t } = useTranslation("workflows");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={target ? t("dsl.import.intoTitle") : t("dsl.import.title")}
        description={
          target
            ? t("dsl.import.intoDescription", { kind: t(`kind.${target.kind}`) })
            : t("dsl.import.description")
        }
        className="w-[min(40rem,calc(100vw-2rem))]"
      >
        <ImportForm target={target} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}
