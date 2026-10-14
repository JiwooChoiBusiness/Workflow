import { Play } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import type { FlowField, VarType, WfIssue, WorkflowKind } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { nodeRunRequest, uploadWorkflowFile, useNodeTypes } from "@/features/workflows/api";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import { nodeById, referencedVariables, type ReferencedVar } from "@/features/workflows/graph";
import "@/features/workflows/i18n";
import {
  serverInputErrors,
  type FieldValue,
  type UploadedFile,
} from "@/features/workflows/runInputs";
import { useWfRunController } from "@/features/workflows/wfRunContext";
import { nodeLabel } from "../labels";
import { RunInputsForm } from "./RunInputsForm";

export function singleRunStorageKey(workflowId: string, nodeId: string): string {
  return `wf.single-run.${workflowId}.${nodeId}`;
}

// 참조 변수의 타입을 입력칸 종류로. 객체·배열·모르는 타입은 JSON(읽지 못하면 글자 그대로)으로 받는다.
function fieldTypeOf(type: VarType | null): FlowField["type"] {
  switch (type) {
    case "string":
      return "paragraph";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "file":
      return "file";
    case "array[file]":
      return "file_list";
    default:
      return "paragraph";
  }
}

function isJsonish(type: VarType | null): boolean {
  return fieldTypeOf(type) === "paragraph" && type !== "string";
}

function loadSaved(key: string): Record<string, FieldValue> {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, FieldValue>)
      : {};
  } catch {
    return {};
  }
}

function save(key: string, values: Record<string, FieldValue>): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(values));
  } catch {
    // 저장소를 쓸 수 없어도 실행은 한다.
  }
}

export function singleRunInputs(
  variables: readonly ReferencedVar[],
  values: Readonly<Record<string, FieldValue>>,
): { inputs: Record<string, unknown>; invalid: string[] } {
  const inputs: Record<string, unknown> = {};
  const invalid: string[] = [];
  for (const variable of variables) {
    const value = values[variable.selector];
    const type = fieldTypeOf(variable.type);
    if (type === "file" || type === "file_list") {
      const files = Array.isArray(value) ? (value as UploadedFile[]) : [];
      if (files.length > 0) {
        inputs[variable.selector] = type === "file" ? files[0]!.id : files.map((file) => file.id);
      }
    } else if (type === "boolean") {
      if (typeof value === "boolean") inputs[variable.selector] = value;
    } else {
      const text = typeof value === "string" ? value : "";
      if (type === "number") {
        if (text.trim() === "") continue;
        const number = Number(text);
        if (Number.isFinite(number)) inputs[variable.selector] = number;
        else invalid.push(variable.selector);
      } else if (variable.type === "string") {
        inputs[variable.selector] = text;
      } else if (text.trim() !== "") {
        try {
          inputs[variable.selector] = JSON.parse(text);
        } catch {
          inputs[variable.selector] = text;
        }
      }
    }
  }
  return { inputs, invalid };
}

/*
 * "이 노드만 실행"(설계 7.3 SingleRunDialog, 6.1). 노드가 참조하는 변수마다 입력칸을 두고, 마지막에 넣은
 * 값을 노드별로 기억한다(wf.single-run.<workflowId>.<nodeId>). 결과는 실행 패널의 노드 런 카드다.
 */
export function SingleRunDialog({
  workflowId,
  kind,
  nodeId,
  onOpenChange,
  flush,
  onStarted,
}: {
  workflowId: string;
  kind: WorkflowKind;
  nodeId: string | null;
  onOpenChange: (open: boolean) => void;
  flush: () => Promise<boolean>;
  onStarted: (nodeId: string) => void;
}) {
  return (
    <Dialog open={nodeId !== null} onOpenChange={onOpenChange}>
      {nodeId ? (
        <SingleRunBody
          key={nodeId}
          workflowId={workflowId}
          kind={kind}
          nodeId={nodeId}
          flush={flush}
          onStarted={(id) => {
            onOpenChange(false);
            onStarted(id);
          }}
          onCancel={() => onOpenChange(false)}
        />
      ) : null}
    </Dialog>
  );
}

function SingleRunBody({
  workflowId,
  kind,
  nodeId,
  flush,
  onStarted,
  onCancel,
}: {
  workflowId: string;
  kind: WorkflowKind;
  nodeId: string;
  flush: () => Promise<boolean>;
  onStarted: (nodeId: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("workflows");
  const controller = useWfRunController();
  const actions = useFlowActions();
  const graph = useFlow((state) => state.graph);
  const conversationVars = useFlow((state) => state.conversationVars);
  const nodeTypes = useNodeTypes();
  const node = nodeById(graph, nodeId);
  const variables = useMemo(
    () =>
      referencedVariables(graph, nodeId, kind, {
        nodeTypes: nodeTypes.data,
        conversationVars,
      }),
    [graph, nodeId, kind, nodeTypes.data, conversationVars],
  );
  const storageKey = singleRunStorageKey(workflowId, nodeId);
  const [values, setValues] = useState<Record<string, FieldValue>>(() => loadSaved(storageKey));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const fields: FlowField[] = variables.map((variable) => ({
    name: variable.selector,
    type: fieldTypeOf(variable.type),
    label: variable.selector,
    required: false,
    default: null,
    options: null,
    max_length: null,
  }));
  const title = node ? node.title || nodeLabel(t, node.type) : nodeId;

  async function submit() {
    const { inputs, invalid } = singleRunInputs(variables, values);
    if (invalid.length > 0) {
      setErrors(Object.fromEntries(invalid.map((key) => [key, t("run.invalidNumber")])));
      return;
    }
    setErrors({});
    setFailure(null);
    setBusy(true);
    try {
      if (!(await flush())) {
        setFailure(new Error(t("run.saveFailed")));
        return;
      }
      const result = await controller.start(() =>
        nodeRunRequest(workflowId, nodeId, { inputs, response_mode: "streaming" }),
      );
      if (result === true) {
        save(storageKey, values);
        onStarted(nodeId);
        return;
      }
      if (isApiError(result, 422) && result.code === "invalid_inputs") {
        setErrors(serverInputErrors(result.details));
      }
      if (
        isApiError(result, 422) &&
        result.code === "draft_invalid" &&
        Array.isArray(result.details)
      ) {
        actions.setIssues(result.details as WfIssue[]);
      }
      setFailure(result);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DialogContent
      title={t("single.title", { name: title })}
      description={t("single.description")}
      className="w-[min(36rem,calc(100vw-2rem))]"
    >
      <form
        className="flex flex-col gap-4"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {variables.length === 0 ? (
          <p className="text-sm text-ink-muted">{t("single.noInputs")}</p>
        ) : (
          <>
            <ul className="flex flex-wrap gap-1.5" aria-label={t("single.types")}>
              {variables.map((variable) => (
                <li key={variable.selector}>
                  <Badge>
                    <span className="font-mono">{variable.selector}</span>
                    {variable.type ? ` · ${variable.type}` : ""}
                  </Badge>
                </li>
              ))}
            </ul>
            <RunInputsForm
              fields={fields}
              values={values}
              onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
              errors={errors}
              upload={(file) => uploadWorkflowFile(workflowId, file)}
              disabled={busy}
            />
            {variables.some((variable) => isJsonish(variable.type)) ? (
              <p className="text-xs text-ink-muted">{t("single.json")}</p>
            ) : null}
          </>
        )}
        {failure ? (
          isApiError(failure, 422) && failure.code === "draft_invalid" ? (
            <Alert tone="danger" title={t("single.draftInvalid")} />
          ) : (
            <ErrorAlert error={failure} title={t("run.startFailed")} />
          )
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onCancel}>
            {t("single.cancel")}
          </Button>
          <Button type="submit" variant="primary" loading={busy}>
            <Play className="size-3.5" aria-hidden />
            {t("single.run")}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
