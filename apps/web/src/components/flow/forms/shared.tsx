import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import {
  get,
  useForm,
  type DefaultValues,
  type FieldValues,
  type UseFormReturn,
} from "react-hook-form";
import type * as z from "zod";
import { Button } from "@/components/ui/button";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import { issuePath, isNodeLevelField, type FlowNode } from "@/features/workflows/graph";

export interface NodeFormProps {
  node: FlowNode;
}

/*
 * 노드 config 폼. 처음 값은 config에서 만들고, 사용자가 고칠 때마다 config로 바꿔 그래프에 넣는다(검증과
 * 관계없이. 초안은 깨진 채로도 저장된다). 실행 취소나 id 변경처럼 폼 밖에서 config가 바뀌면 속성 패널이
 * 폼을 새로 만든다(flowStore.formEpoch).
 */
export function useNodeForm<F extends FieldValues>(
  node: FlowNode,
  schema: z.ZodType<F, F>,
  toForm: (config: Record<string, unknown>) => F,
  fromForm: (values: F) => Record<string, unknown>,
): UseFormReturn<F> {
  const actions = useFlowActions();
  const nodeId = node.id;
  const [defaultValues] = useState(() => toForm(node.config) as DefaultValues<F>);
  const form = useForm<F>({ defaultValues, resolver: zodResolver(schema), mode: "onChange" });
  useEffect(() => {
    // 저장된 config에 이미 있는 문제도 바로 보이게 한다.
    void form.trigger();
    return form.subscribe({
      formState: { values: true },
      callback: (info) => {
        // reset처럼 사용자가 고치지 않은 변화는 그래프에 넣지 않는다.
        if (info.name === undefined) return;
        actions.updateConfig(nodeId, fromForm(form.getValues()));
      },
    });
  }, [form, actions, nodeId, fromForm]);
  return form;
}

// 서버 검증 이슈 중 이 노드의 config 칸에 붙는 것. 경로는 폼 경로("cases.0.conditions.1.var")다.
export function useServerIssue(nodeId: string): (path: string) => string | undefined {
  const issues = useFlow((state) => state.issues);
  return (path: string) => {
    const found = issues.find(
      (issue) =>
        issue.node_id === nodeId &&
        issue.field &&
        !isNodeLevelField(issue.field) &&
        issuePath(issue.field) === path,
    );
    return found?.message;
  };
}

// 폼(zod) 오류가 먼저, 없으면 같은 칸의 서버 이슈.
export function errorAt<F extends FieldValues>(
  form: UseFormReturn<F>,
  path: string,
  serverIssue: (path: string) => string | undefined,
): string | undefined {
  const error = get(form.formState.errors, path) as
    { message?: string; root?: { message?: string } } | undefined;
  return error?.message ?? error?.root?.message ?? serverIssue(path);
}

export function Section({
  title,
  hint,
  actions,
  children,
}: {
  title: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2.5 border-t border-line pt-4 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          {hint ? <p className="mt-0.5 text-xs text-ink-muted">{hint}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button size="sm" variant="secondary" onClick={onClick}>
      <Plus className="size-3.5" aria-hidden />
      {label}
    </Button>
  );
}

export function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button size="iconSm" variant="dangerGhost" aria-label={label} title={label} onClick={onClick}>
      <Trash2 className="size-4" aria-hidden />
    </Button>
  );
}

export function FieldMessage({ message }: { message: string | undefined }) {
  if (!message) return null;
  return (
    <p className="text-xs text-danger" role="alert">
      {message}
    </p>
  );
}

export function EmptyRows({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-line px-3 py-3 text-center text-xs text-ink-muted">
      {children}
    </p>
  );
}
