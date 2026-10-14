import { Download, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { AssistantMessage } from "@/components/chat/AssistantMessage";
import { JsonTree } from "@/components/common/JsonTree";
import { CopyButton } from "@/components/ui/copy-button";
import "@/features/workflows/i18n";
import { formatBytes } from "@/lib/format";

// 변수 풀의 파일 값(서버 FileRef). 이 모양이면 내려받기 링크로 그린다.
export interface FileValue {
  file_id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  source: string;
}

export function isFileValue(value: unknown): value is FileValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.file_id === "string" &&
    typeof item.filename === "string" &&
    typeof item.size_bytes === "number"
  );
}

export type FileUrl = (fileId: string) => string | null;

function FileLink({ file, fileUrl }: { file: FileValue; fileUrl?: FileUrl }) {
  const { t } = useTranslation("workflows");
  const href = fileUrl?.(file.file_id) ?? null;
  return (
    <li className="flex items-center gap-2 text-sm">
      <FileText className="size-4 shrink-0 text-ink-subtle" aria-hidden />
      {href ? (
        <a
          href={href}
          download
          className="inline-flex min-w-0 items-center gap-1 text-accent hover:underline"
          aria-label={t("outputs.download", { name: file.filename })}
        >
          <span className="truncate">{file.filename}</span>
          <Download className="size-3.5 shrink-0" aria-hidden />
        </a>
      ) : (
        <span className="min-w-0 truncate text-ink" title={t("outputs.noDownload")}>
          {file.filename}
        </span>
      )}
      <span className="shrink-0 text-xs text-ink-subtle tabular-nums">
        {formatBytes(file.size_bytes)}
      </span>
    </li>
  );
}

function OutputValue({ value, fileUrl }: { value: unknown; fileUrl?: FileUrl }) {
  const { t } = useTranslation("workflows");
  if (isFileValue(value)) {
    return (
      <ul>
        <FileLink file={value} fileUrl={fileUrl} />
      </ul>
    );
  }
  if (Array.isArray(value) && value.length > 0 && value.every(isFileValue)) {
    return (
      <ul className="flex flex-col gap-1">
        {value.map((file) => (
          <FileLink key={file.file_id} file={file} fileUrl={fileUrl} />
        ))}
      </ul>
    );
  }
  if (typeof value === "string") {
    return value ? (
      <AssistantMessage text={value} streaming={false} />
    ) : (
      <p className="text-sm text-ink-subtle">{t("outputs.emptyString")}</p>
    );
  }
  if (value === null || value === undefined) {
    return <p className="font-mono text-xs text-ink-subtle">null</p>;
  }
  if (typeof value !== "object") {
    return <p className="font-mono text-sm text-ink">{String(value)}</p>;
  }
  return (
    <div className="max-h-96 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs text-code-ink">
      <JsonTree value={value} />
    </div>
  );
}

function copyText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/*
 * 런 결과(end 노드 출력을 합친 것, 설계 7.3·7.4). 글은 마크다운, 객체·배열은 JSON, 파일은 내려받기
 * 링크다. fileUrl이 없거나 null을 돌려주면 이름과 크기만 보인다.
 */
export function OutputsView({
  outputs,
  truncated = false,
  fileUrl,
  className,
}: {
  outputs: Record<string, unknown> | null;
  truncated?: boolean;
  fileUrl?: FileUrl;
  className?: string;
}) {
  const { t } = useTranslation("workflows");
  const entries = Object.entries(outputs ?? {});
  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">{t("outputs.empty")}</p>;
  }
  return (
    <div className={className}>
      {truncated ? <p className="mb-2 text-xs text-warn">{t("outputs.truncated")}</p> : null}
      <dl className="flex flex-col gap-3">
        {entries.map(([name, value]) => (
          <div key={name} className="flex flex-col gap-1.5">
            <dt className="flex items-center justify-between gap-2">
              <span className="font-mono text-xs font-medium text-ink-muted">{name}</span>
              {!isFileValue(value) ? (
                <CopyButton
                  value={copyText(value)}
                  size="iconSm"
                  variant="ghost"
                  label={t("outputs.copy", { name })}
                />
              ) : null}
            </dt>
            <dd className="min-w-0">
              <OutputValue value={value} fileUrl={fileUrl} />
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
