import { CircleAlert, MousePointerClick, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { EmptyState } from "@/components/ui/layout";
import { useNodeTypes } from "@/features/workflows/api";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import {
  ITERATION_START_TYPE,
  nodeById,
  nodeOutputs,
  type FlowNode,
} from "@/features/workflows/graph";
import { nodeMeta, TONE_CLASS } from "@/features/workflows/nodeTypes";
import { cn } from "@/lib/cn";
import { ErrorRetrySection } from "./ErrorRetrySection";
import { NODE_FORMS } from "./forms";
import { nodeDescription, nodeLabel } from "./labels";
import { NodeIdField } from "./NodeIdField";

function TitleField({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  return (
    <Field label={t("inspector.title")}>
      <Input
        value={node.title}
        maxLength={200}
        onChange={(event) =>
          actions.updateNode(node.id, { title: event.target.value }, `title:${node.id}`)
        }
      />
    </Field>
  );
}

// 모르는 노드 타입(서버가 새로 알려 준 타입)은 config를 JSON으로 고친다. 객체로 읽힐 때만 그래프에 넣는다.
function JsonConfigForm({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const actions = useFlowActions();
  const [text, setText] = useState(() => JSON.stringify(node.config, null, 2));
  const [error, setError] = useState<string | undefined>(undefined);
  return (
    <Field label={t("inspector.config")} hint={t("inspector.unknownType")} error={error}>
      <CodeEditor
        value={text}
        language="json"
        ariaLabel={t("inspector.config")}
        minHeight={200}
        maxHeight={480}
        onChange={(next) => {
          setText(next);
          try {
            const parsed: unknown = JSON.parse(next);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
              setError(tc("validation.jsonObject"));
              return;
            }
            setError(undefined);
            actions.updateConfig(node.id, parsed as Record<string, unknown>);
          } catch {
            setError(t("validation.json"));
          }
        }}
      />
    </Field>
  );
}

function NodeIssues({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("workflows");
  const issues = useFlow((state) => state.issues);
  const mine = issues.filter((issue) => issue.node_id === nodeId);
  if (mine.length === 0) return null;
  return (
    <section aria-label={t("inspector.issues")} className="flex flex-col gap-1">
      {mine.map((issue, index) => {
        const Icon = issue.level === "error" ? CircleAlert : TriangleAlert;
        return (
          <p
            key={`${issue.code}-${issue.field ?? ""}-${index}`}
            className={cn(
              "flex items-start gap-1.5 rounded-md px-2 py-1.5 text-xs",
              issue.level === "error" ? "bg-danger-soft text-ink" : "bg-warn-soft text-ink",
            )}
          >
            <Icon
              className={cn(
                "mt-0.5 size-3.5 shrink-0",
                issue.level === "error" ? "text-danger" : "text-warn",
              )}
              aria-label={issue.level === "error" ? t("issues.error") : t("issues.warning")}
            />
            <span className="min-w-0">
              {issue.message}
              {issue.field ? (
                <span className="ml-1 font-mono text-ink-muted">({issue.field})</span>
              ) : null}
            </span>
          </p>
        );
      })}
    </section>
  );
}

function SingleNode({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const kind = useFlow((state) => state.kind);
  const formEpoch = useFlow((state) => state.formEpoch);
  const nodeTypes = useNodeTypes();
  const info = nodeTypes.data?.find((item) => item.type === node.type);
  const meta = nodeMeta(node.type);
  const Icon = meta.icon;
  const Form = NODE_FORMS[node.type];
  const entry = node.type === ITERATION_START_TYPE;
  const outputs = nodeOutputs(node, info);

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            "grid size-8 shrink-0 place-items-center rounded-md",
            TONE_CLASS[meta.tone],
          )}
        >
          <Icon className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-ink">{nodeLabel(t, node.type)}</h2>
          <p className="text-xs text-ink-muted">{nodeDescription(t, node.type)}</p>
        </div>
        {entry ? null : (
          <Button
            size="iconSm"
            variant="dangerGhost"
            aria-label={t("inspector.delete")}
            title={t("inspector.delete")}
            onClick={() => actions.remove([node.id], [])}
          >
            <Trash2 className="size-4" aria-hidden />
          </Button>
        )}
      </div>
      {info && !info.implemented && !entry ? (
        <Alert tone="warn" title={t("inspector.notImplemented")} />
      ) : null}
      {info && !info.kinds.includes(kind) ? (
        <Alert
          tone="danger"
          title={t("inspector.kindOnly", { kind: t(`kind.${info.kinds[0] ?? "workflow"}`) })}
        />
      ) : null}
      <NodeIssues nodeId={node.id} />
      <div className="grid gap-3">
        <TitleField node={node} />
        <NodeIdField key={node.id} node={node} />
      </div>
      {entry ? (
        <p className="rounded-md bg-sunken px-3 py-2 text-xs text-ink-muted">
          {t("inspector.entry", { id: node.id })}
        </p>
      ) : Form ? (
        <Form key={`form:${node.id}:${formEpoch}`} node={node} />
      ) : (
        <JsonConfigForm key={`json:${node.id}:${formEpoch}`} node={node} />
      )}
      {outputs.length > 0 ? (
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-ink-muted">{t("inspector.outputs")}</h3>
          <div className="flex flex-wrap gap-1">
            {outputs.map((output) => (
              <Badge key={output.name} className="font-mono" title={output.selector}>
                {output.name}: {output.type}
              </Badge>
            ))}
          </div>
        </section>
      ) : null}
      {entry ? null : (
        <ErrorRetrySection key={`retry:${node.id}:${formEpoch}`} node={node} info={info} />
      )}
    </div>
  );
}

// 오른쪽 패널의 노드 탭(설계 7.3 속성 패널). 고른 노드 하나의 폼만 그린다.
export function NodeInspector() {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const selection = useFlow((state) => state.selection);
  const node = useFlow((state) =>
    state.selection.length === 1 ? nodeById(state.graph, state.selection[0]!) : undefined,
  );
  if (selection.length > 1) {
    return (
      <div className="p-4">
        <EmptyState
          icon={MousePointerClick}
          title={t("inspector.multi", { count: selection.length })}
          action={
            <Button variant="danger" size="sm" onClick={() => actions.remove(selection, [])}>
              <Trash2 className="size-3.5" aria-hidden />
              {t("inspector.deleteSelected")}
            </Button>
          }
        />
      </div>
    );
  }
  if (!node) {
    return (
      <div className="p-4">
        <EmptyState
          icon={MousePointerClick}
          title={t("inspector.none")}
          hint={t("inspector.noneHint")}
        />
      </div>
    );
  }
  return <SingleNode node={node} />;
}
