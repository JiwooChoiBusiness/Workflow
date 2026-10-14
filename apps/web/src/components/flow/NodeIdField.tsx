import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";
import { nodeIdProblem, type FlowNode } from "@/features/workflows/graph";

// 노드 id 칸. 바꾸면 엣지와 다른 노드의 변수 참조({{ id.x }}, "id.x")를 함께 고친다(설계 2.2, 11.1 Q11).
// id가 바뀌면 속성 패널이 이 칸을 새로 만든다(key).
export function NodeIdField({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const graph = useFlow((state) => state.graph);
  const serverIssue = useFlow(
    (state) =>
      state.issues.find((issue) => issue.node_id === node.id && issue.field === "id")?.message,
  );
  const [draft, setDraft] = useState(node.id);
  const value = draft.trim();
  const problem = nodeIdProblem(graph, node.id, value);

  function commit() {
    if (value === node.id) {
      setDraft(node.id);
      return;
    }
    if (problem) return;
    actions.rename(node.id, value);
    toast.success(t("nodeId.renamed", { to: value }));
  }

  return (
    <Field
      label={t("nodeId.label")}
      hint={t("nodeId.hint")}
      error={problem ? t(`nodeId.${problem}`) : serverIssue}
    >
      <Input
        className="font-mono"
        value={draft}
        spellCheck={false}
        autoComplete="off"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          } else if (event.key === "Escape") {
            setDraft(node.id);
          }
        }}
      />
    </Field>
  );
}
