import { lazy, Suspense } from "react";
import { useFieldControl } from "@/components/ui/field";
import { useVariableGroups, type VarGroup } from "@/features/workflows/useUpstream";
import type { PromptEditorCoreProps } from "./PromptEditorCore";

// CodeMirror는 속성 패널을 처음 열 때 받는다.
const PromptEditorCore = lazy(() => import("./PromptEditorCore"));

export type PromptEditorProps = Omit<PromptEditorCoreProps, "groups"> & {
  nodeId: string;
  // template 노드처럼 변수 풀 대신 정해진 이름만 쓰는 칸
  groups?: VarGroup[];
};

export function PromptEditor({ nodeId, groups, ...props }: PromptEditorProps) {
  const upstream = useVariableGroups(nodeId);
  const field = useFieldControl({ id: props.id, "aria-describedby": props.ariaDescribedBy });
  const height = props.singleLine ? 34 : (props.minHeight ?? 72);
  return (
    <Suspense
      fallback={
        <div
          className="rounded-md border border-line bg-sunken"
          style={{ minHeight: height }}
          aria-hidden
        />
      }
    >
      <PromptEditorCore
        {...props}
        id={field.id}
        groups={groups ?? upstream}
        allowCustom={!groups}
        ariaDescribedBy={field["aria-describedby"]}
        invalid={props.invalid ?? field["aria-invalid"] === true}
      />
    </Suspense>
  );
}
