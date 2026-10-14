import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { EditorState, type Extension } from "@codemirror/state";
import { EditorView, lineNumbers, placeholder as placeholderText } from "@codemirror/view";
import { minimalSetup } from "codemirror";
import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";

// python은 강조 없이 줄바꿈만 끈다(파이썬 문법 패키지를 넣지 않았다).
export type CodeLanguage = "markdown" | "json" | "text" | "python";

export interface CodeEditorProps {
  value: string;
  onChange?: (value: string) => void;
  language?: CodeLanguage;
  readOnly?: boolean;
  minHeight?: number;
  maxHeight?: number;
  placeholder?: string;
  ariaLabel: string;
  ariaDescribedBy?: string;
  invalid?: boolean;
  className?: string;
}

// 색은 토큰을 그대로 쓰므로 밝은·어두운 테마를 따로 두지 않는다.
const baseTheme = EditorView.theme({
  "&": {
    backgroundColor: "var(--color-surface)",
    color: "var(--color-ink)",
    fontSize: "13px",
  },
  ".cm-content": {
    fontFamily: "var(--font-mono)",
    caretColor: "var(--color-ink)",
    padding: "8px 0",
  },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.55" },
  ".cm-gutters": {
    backgroundColor: "var(--color-sunken)",
    color: "var(--color-ink-subtle)",
    border: "none",
    borderRight: "1px solid var(--color-line)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--color-accent-soft) !important",
  },
  ".cm-placeholder": { color: "var(--color-ink-subtle)" },
});

function languageExtension(language: CodeLanguage): Extension[] {
  if (language === "markdown") return [markdown(), EditorView.lineWrapping];
  if (language === "json") return [json()];
  if (language === "python") return [];
  return [EditorView.lineWrapping];
}

export default function CodeEditor({
  value,
  onChange,
  language = "text",
  readOnly = false,
  minHeight = 160,
  maxHeight,
  placeholder,
  ariaLabel,
  ariaDescribedBy,
  invalid,
  className,
}: CodeEditorProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);

  useEffect(() => {
    valueRef.current = value;
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: valueRef.current,
        extensions: [
          minimalSetup,
          lineNumbers(),
          baseTheme,
          ...languageExtension(language),
          EditorState.readOnly.of(readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.contentAttributes.of({
            "aria-label": ariaLabel,
            ...(ariaDescribedBy ? { "aria-describedby": ariaDescribedBy } : {}),
          }),
          placeholder ? placeholderText(placeholder) : [],
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current?.(update.state.doc.toString());
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [language, readOnly, placeholder, ariaLabel, ariaDescribedBy]);

  // 폼 reset처럼 밖에서 값이 바뀌면 에디터 내용을 맞춘다. 입력 중 같은 값이면 건드리지 않는다.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

  return (
    <div
      ref={hostRef}
      className={cn(
        "overflow-auto rounded-md border border-line bg-surface focus-within:border-accent focus-within:ring-2 focus-within:ring-focus",
        invalid && "border-danger",
        className,
      )}
      style={{ minHeight, maxHeight }}
    />
  );
}
