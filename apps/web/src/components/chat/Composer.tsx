import { Paperclip, SendHorizontal, Square, X } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { formatBytes } from "@/lib/format";

const MAX_HEIGHT = 200;
// 서버 한도(apps/api/app/schemas/session.py, services/sessions.py): 메시지당 20개, 파일 하나 100MB
export const MAX_ATTACHMENTS = 20;
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;

export type SendResult = boolean | void;

export function Composer({
  onSend,
  onStop,
  running,
  stoppable = false,
  stopping = false,
  disabled = false,
  answerMode = false,
  attachments = false,
}: {
  // false를 돌려주면(서버가 받지 않음) 입력한 글과 파일을 되돌려 놓는다.
  onSend: (text: string, files: File[]) => SendResult | Promise<SendResult>;
  onStop: () => void;
  // 런이 도는 중이면 보내기 대신 중지 버튼을 둔다.
  running: boolean;
  // 입력을 기다리는 런처럼 돌지는 않지만 멈출 수 있으면 보내기 옆에 중지 버튼을 둔다.
  stoppable?: boolean;
  stopping?: boolean;
  disabled?: boolean;
  // 질문이 걸려 있으면 보낸 글이 답으로 쓰인다(설계 4.9). 답에는 파일을 붙일 수 없다.
  answerMode?: boolean;
  attachments?: boolean;
}) {
  const { t } = useTranslation("chat");
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const locked = running || disabled;
  const canAttach = attachments && !answerMode;

  function resize() {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }

  async function send() {
    const value = text.trim();
    if (!value || locked) return;
    const picked = canAttach ? files : [];
    setText("");
    setFiles([]);
    setFileError(null);
    requestAnimationFrame(resize);
    const result = await onSend(value, picked);
    if (result === false) {
      // 그사이에 새로 쓴 글이 있으면 덮지 않는다.
      setText((current) => current || value);
      setFiles((current) => (current.length > 0 ? current : picked));
      requestAnimationFrame(resize);
    }
  }

  function addFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    const picked = Array.from(list);
    const tooLarge = picked.find((file) => file.size > MAX_ATTACHMENT_BYTES);
    const accepted = picked.filter((file) => file.size <= MAX_ATTACHMENT_BYTES);
    const next = [...files, ...accepted];
    setFiles(next.slice(0, MAX_ATTACHMENTS));
    if (tooLarge) setFileError(t("composer.tooLarge", { name: tooLarge.name }));
    else if (next.length > MAX_ATTACHMENTS)
      setFileError(t("composer.tooMany", { max: MAX_ATTACHMENTS }));
    else setFileError(null);
  }

  return (
    <div className="border-t border-line bg-surface p-3">
      {canAttach && files.length > 0 ? (
        <ul aria-label={t("composer.attachments")} className="mb-2 flex flex-wrap gap-1.5">
          {files.map((file, index) => (
            <li
              key={`${file.name}:${index}`}
              className="inline-flex max-w-full items-center gap-1 rounded-md border border-line bg-sunken py-0.5 pr-0.5 pl-2 text-xs text-ink"
            >
              <span className="min-w-0 truncate">{file.name}</span>
              <span className="shrink-0 text-ink-subtle tabular-nums">
                {formatBytes(file.size)}
              </span>
              <Button
                variant="ghost"
                size="iconSm"
                className="size-5"
                aria-label={t("composer.removeFile", { name: file.name })}
                disabled={locked}
                onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}
              >
                <X className="size-3" aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
      {fileError ? <p className="mb-2 text-xs text-danger">{fileError}</p> : null}
      <div
        className={cn(
          "flex items-end gap-2 rounded-lg border border-line bg-surface px-2 py-1.5",
          "focus-within:border-accent focus-within:ring-2 focus-within:ring-focus",
          locked && "bg-sunken",
        )}
      >
        {canAttach ? (
          <>
            <input
              ref={fileRef}
              type="file"
              multiple
              tabIndex={-1}
              className="sr-only"
              aria-label={t("composer.fileInput")}
              onChange={(event) => {
                addFiles(event.target.files);
                event.target.value = "";
              }}
            />
            <Button
              variant="ghost"
              size="iconSm"
              aria-label={t("composer.attach")}
              title={t("composer.attach")}
              disabled={locked}
              onClick={() => fileRef.current?.click()}
            >
              <Paperclip className="size-4" aria-hidden />
            </Button>
          </>
        ) : null}
        <textarea
          ref={ref}
          rows={1}
          value={text}
          disabled={locked}
          aria-label={t("composer.label")}
          placeholder={answerMode ? t("composer.answerPlaceholder") : t("composer.placeholder")}
          className="max-h-[200px] min-h-7 flex-1 resize-none bg-transparent py-1 text-sm text-ink outline-none placeholder:text-ink-subtle disabled:cursor-not-allowed"
          onChange={(event) => {
            setText(event.target.value);
            resize();
          }}
          onKeyDown={(event) => {
            // 한글 입력 중(조합 중)의 Enter는 글자 확정이라 보내지 않는다.
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
        />
        {running || stoppable ? (
          <Button variant="secondary" size="sm" loading={stopping} onClick={onStop}>
            {stopping ? null : <Square className="size-3.5" aria-hidden />}
            {t("composer.stop")}
          </Button>
        ) : null}
        {running ? null : (
          <Button
            variant="primary"
            size="sm"
            disabled={locked || !text.trim()}
            onClick={() => void send()}
          >
            <SendHorizontal className="size-3.5" aria-hidden />
            {t("composer.send")}
          </Button>
        )}
      </div>
    </div>
  );
}
