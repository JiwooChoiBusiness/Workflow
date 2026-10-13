import { ShieldAlert } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { WaitingInputData } from "@/api/run-events";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/toggle";
import { maskedHeaders, prettyJson } from "./toolSummary";

export interface ApprovalPromptProps {
  interaction: WaitingInputData;
  onApprove: (options: { sessionAutoApprove: boolean }) => void;
  onReject: (note: string) => void;
  busy?: boolean;
}

function Block({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-ink-muted">{label}</p>
      {children}
    </div>
  );
}

// 명령·코드는 자르지 않고 그대로 보여 준다. 승인하는 내용과 실행되는 내용이 같아야 한다(설계 3.10, T16).
function Code({ children }: { children: string }) {
  return (
    <pre className="max-h-96 overflow-auto rounded-md bg-term px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all text-term-ink">
      {children}
    </pre>
  );
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : value === undefined ? "" : prettyJson(value);
}

function ToolDetail({ name, input }: { name: string; input: Record<string, unknown> }) {
  const { t } = useTranslation("chat");
  switch (name) {
    case "shell":
      return (
        <Block label={t("approval.command")}>
          <Code>{asText(input.command)}</Code>
        </Block>
      );
    case "run_python":
      return (
        <Block label={t("approval.code")}>
          <Code>{asText(input.code)}</Code>
        </Block>
      );
    case "install_packages": {
      const packages = Array.isArray(input.packages) ? input.packages.map(String) : [];
      return (
        <Block label={t("approval.packages")}>
          <ul className="flex flex-wrap gap-1.5">
            {packages.map((pkg) => (
              <li
                key={pkg}
                className="rounded-sm bg-code px-1.5 py-0.5 font-mono text-xs text-code-ink"
              >
                {pkg}
              </li>
            ))}
          </ul>
          {input.upgrade ? (
            <p className="mt-1 text-xs text-ink-muted">{t("approval.upgrade")}</p>
          ) : null}
        </Block>
      );
    }
    case "http_request": {
      const headers = maskedHeaders(input.headers);
      return (
        <div className="flex flex-col gap-2">
          <Block label={`${t("approval.method")} · ${t("approval.url")}`}>
            <Code>{`${asText(input.method) || "GET"} ${asText(input.url)}`}</Code>
          </Block>
          {headers.length > 0 ? (
            <Block label={t("approval.headers")}>
              <Code>{headers.map(([key, value]) => `${key}: ${value}`).join("\n")}</Code>
            </Block>
          ) : null}
          {input.body !== undefined && input.body !== null && input.body !== "" ? (
            <Block label={t("approval.body")}>
              <Code>{asText(input.body)}</Code>
            </Block>
          ) : null}
        </div>
      );
    }
    default:
      return (
        <Block label={t("approval.input")}>
          <Code>{prettyJson(input)}</Code>
        </Block>
      );
  }
}

export function ApprovalPrompt({
  interaction,
  onApprove,
  onReject,
  busy = false,
}: ApprovalPromptProps) {
  const { t } = useTranslation("chat");
  const [note, setNote] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const titleId = useId();
  const noteId = useId();
  const autoId = useId();
  const input = interaction.input ?? {};
  const toolName = interaction.tool_name ?? "";
  const timeout =
    interaction.timeout_sec ?? (typeof input.timeout_sec === "number" ? input.timeout_sec : null);

  return (
    <section
      aria-labelledby={titleId}
      className="flex flex-col gap-3 rounded-md border border-warn/60 bg-warn-soft/60 p-3"
    >
      <header className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden />
        <div>
          <h3 id={titleId} className="text-sm font-semibold text-ink">
            {t("approval.title")}
          </h3>
          <p className="text-xs text-ink-muted">{t("approval.description")}</p>
        </div>
      </header>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs">
        <dt className="text-ink-muted">{t("approval.tool")}</dt>
        <dd className="font-mono text-ink">{toolName || "-"}</dd>
        {interaction.cwd ? (
          <>
            <dt className="text-ink-muted">{t("approval.cwd")}</dt>
            <dd className="font-mono break-all text-ink">{interaction.cwd}</dd>
          </>
        ) : null}
        {timeout !== null ? (
          <>
            <dt className="text-ink-muted">{t("approval.timeout")}</dt>
            <dd className="text-ink">{t("approval.timeoutValue", { seconds: timeout })}</dd>
          </>
        ) : null}
      </dl>
      <ToolDetail name={toolName} input={input} />
      <div>
        <label htmlFor={noteId} className="mb-1 block text-xs font-medium text-ink-muted">
          {t("approval.rejectNote")}
        </label>
        <Textarea
          id={noteId}
          rows={2}
          className="min-h-0 bg-surface"
          value={note}
          placeholder={t("approval.rejectNotePlaceholder")}
          disabled={busy}
          onChange={(event) => setNote(event.target.value)}
        />
      </div>
      <div className="flex items-start gap-2">
        <Checkbox
          id={autoId}
          className="mt-0.5"
          checked={autoApprove}
          disabled={busy}
          onCheckedChange={(value) => setAutoApprove(value === true)}
          aria-describedby={`${autoId}-hint`}
        />
        <div>
          <label htmlFor={autoId} className="text-sm text-ink">
            {t("approval.autoApprove")}
          </label>
          <p id={`${autoId}-hint`} className="text-xs text-ink-muted">
            {t("approval.autoApproveHint")}
          </p>
        </div>
      </div>
      {/* 키보드 단축키와 자동 포커스는 두지 않는다. 잘못 승인하는 일을 막는다(설계 3.10). */}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" disabled={busy} onClick={() => onReject(note.trim())}>
          {t("approval.reject")}
        </Button>
        <Button
          variant="primary"
          disabled={busy}
          onClick={() => onApprove({ sessionAutoApprove: autoApprove })}
        >
          {t("approval.approve")}
        </Button>
      </div>
    </section>
  );
}
