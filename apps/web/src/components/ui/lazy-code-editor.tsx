import { lazy, Suspense } from "react";
import { useFieldControl } from "./field";
import type { CodeEditorProps } from "./code-editor";

// CodeMirror는 빌더·스킬 화면에서만 쓰므로 처음 묶음에서 뺀다.
const CodeEditorImpl = lazy(() => import("./code-editor"));

export function CodeEditor(props: CodeEditorProps & { id?: string }) {
  const field = useFieldControl({ id: props.id, "aria-describedby": props.ariaDescribedBy });
  return (
    <Suspense
      fallback={
        <div
          className="rounded-md border border-line bg-sunken"
          style={{ minHeight: props.minHeight ?? 160 }}
          aria-hidden
        />
      }
    >
      <CodeEditorImpl
        {...props}
        ariaDescribedBy={field["aria-describedby"]}
        invalid={props.invalid ?? field["aria-invalid"] === true}
      />
    </Suspense>
  );
}
