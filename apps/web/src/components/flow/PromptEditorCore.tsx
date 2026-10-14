import { EditorState } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  MatchDecorator,
  placeholder as placeholderText,
  ViewPlugin,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { minimalSetup } from "codemirror";
import { Braces } from "lucide-react";
import { Popover } from "radix-ui";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import type { VarGroup } from "@/features/workflows/useUpstream";
import { cn } from "@/lib/cn";
import { VariableList } from "./VariablePicker";

export interface PromptEditorCoreProps {
  value: string;
  onChange: (value: string) => void;
  groups: VarGroup[];
  ariaLabel: string;
  ariaDescribedBy?: string;
  invalid?: boolean;
  minHeight?: number;
  maxHeight?: number;
  placeholder?: string;
  // URL처럼 한 줄 칸이면 줄바꿈을 막는다.
  singleLine?: boolean;
  id?: string;
  // 정해진 이름만 쓰는 칸(template 노드)은 선택자 직접 입력을 두지 않는다.
  allowCustom?: boolean;
}

const varMatcher = new MatchDecorator({
  regexp: /\{\{[^{}\n]*\}\}|\{%[^{}\n]*%\}/g,
  decoration: Decoration.mark({ class: "wf-var-token" }),
});

const varHighlight = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = varMatcher.createDeco(view);
    }
    update(update: ViewUpdate) {
      this.decorations = varMatcher.updateDeco(update, this.decorations);
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

const theme = EditorView.theme({
  "&": { backgroundColor: "var(--color-surface)", color: "var(--color-ink)", fontSize: "13px" },
  // 프롬프트는 대부분 글이라 본문 글꼴로 쓴다. 변수 표시는 wf-var-token이 따로 꾸민다.
  ".cm-content": {
    fontFamily: "var(--font-sans)",
    caretColor: "var(--color-ink)",
    padding: "6px 0",
  },
  // 오른쪽 위 "변수 넣기" 단추와 글이 겹치지 않게 비워 둔다.
  ".cm-line": { padding: "0 30px 0 8px" },
  ".cm-scroller": { fontFamily: "var(--font-sans)", lineHeight: "1.55" },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--color-accent-soft) !important",
  },
  ".cm-placeholder": { color: "var(--color-ink-subtle)" },
});

const singleLineFilter = EditorState.transactionFilter.of((transaction) =>
  transaction.newDoc.lines > 1 ? [] : transaction,
);

interface PickerState {
  // 넣을 자리. typed면 방금 친 {{ 바로 뒤라 " 선택자 }}"만 넣는다.
  from: number;
  to: number;
  typed: boolean;
  left: number;
  top: number;
}

// 템플릿 칸(프롬프트, URL, 본문 등) 편집기. {{ }} 변수를 강조하고 {{를 치면 변수 목록을 연다(설계 7.3).
export default function PromptEditorCore({
  value,
  onChange,
  groups,
  ariaLabel,
  ariaDescribedBy,
  invalid,
  minHeight = 72,
  maxHeight = 320,
  placeholder,
  singleLine = false,
  id,
  allowCustom = true,
}: PromptEditorCoreProps) {
  const { t } = useTranslation("workflows");
  const wrapRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  const [picker, setPicker] = useState<PickerState | null>(null);

  useEffect(() => {
    valueRef.current = value;
    onChangeRef.current = onChange;
  });

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const openAt = (view: EditorView, from: number, to: number, typed: boolean) => {
      const coords = view.coordsAtPos(to);
      const box = wrapRef.current?.getBoundingClientRect();
      if (!coords || !box) return;
      setPicker({
        from,
        to,
        typed,
        left: coords.left - box.left,
        top: coords.bottom - box.top,
      });
    };
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: valueRef.current,
        extensions: [
          minimalSetup,
          theme,
          varHighlight,
          singleLine ? singleLineFilter : EditorView.lineWrapping,
          EditorView.contentAttributes.of({
            "aria-label": ariaLabel,
            ...(ariaDescribedBy ? { "aria-describedby": ariaDescribedBy } : {}),
            ...(id ? { id } : {}),
            ...(singleLine ? {} : { "aria-multiline": "true" }),
          }),
          placeholder ? placeholderText(placeholder) : [],
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            onChangeRef.current(update.state.doc.toString());
            const typed = update.transactions.some((tr) => tr.isUserEvent("input.type"));
            const head = update.state.selection.main.head;
            if (typed && update.state.sliceDoc(head - 2, head) === "{{") {
              openAt(update.view, head, head, true);
            }
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [ariaLabel, ariaDescribedBy, placeholder, singleLine, id]);

  // 폼 reset·실행 취소처럼 밖에서 값이 바뀌면 편집기를 맞춘다. 입력 중 같은 값이면 건드리지 않는다.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

  function insert(selector: string) {
    const view = viewRef.current;
    if (!view || !picker) return;
    const text = picker.typed ? ` ${selector} }}` : `{{ ${selector} }}`;
    view.dispatch({
      changes: { from: picker.from, to: picker.to, insert: text },
      selection: { anchor: picker.from + text.length },
    });
    setPicker(null);
    view.focus();
  }

  function openFromButton() {
    const view = viewRef.current;
    if (!view) return;
    const { from, to } = view.state.selection.main;
    const coords = view.coordsAtPos(to) ?? view.coordsAtPos(view.state.doc.length);
    const box = wrapRef.current?.getBoundingClientRect();
    setPicker({
      from,
      to,
      typed: false,
      left: coords && box ? coords.left - box.left : 0,
      top: coords && box ? coords.bottom - box.top : 0,
    });
  }

  return (
    <div ref={wrapRef} className="relative">
      <div
        ref={hostRef}
        className={cn(
          "overflow-auto rounded-md border border-line bg-surface focus-within:border-accent focus-within:ring-2 focus-within:ring-focus",
          invalid && "border-danger",
        )}
        style={{ minHeight: singleLine ? undefined : minHeight, maxHeight }}
      />
      <div className="absolute top-1 right-1">
        <Button
          size="iconSm"
          variant="ghost"
          aria-label={t("prompt.insert")}
          title={`${t("prompt.insert")} · ${t("prompt.hint", { open: "{{" })}`}
          onClick={openFromButton}
        >
          <Braces className="size-3.5" aria-hidden />
        </Button>
      </div>
      <Popover.Root
        open={picker !== null}
        onOpenChange={(open) => {
          if (!open) setPicker(null);
        }}
      >
        <Popover.Anchor asChild>
          <span
            aria-hidden
            className="pointer-events-none absolute size-px"
            style={{ left: picker?.left ?? 0, top: picker?.top ?? 0 }}
          />
        </Popover.Anchor>
        <Popover.Portal>
          <Popover.Content
            align="start"
            sideOffset={4}
            collisionPadding={8}
            className="z-50 overflow-hidden rounded-md border border-line bg-surface shadow-lg"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              viewRef.current?.focus();
            }}
          >
            <VariableList groups={groups} onPick={insert} allowCustom={allowCustom} />
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
