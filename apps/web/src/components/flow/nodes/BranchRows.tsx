import { Handle, Position } from "@xyflow/react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { IF_ELSE_OPS, OP_KEYS, type IfElseOp } from "@/features/workflows/configSchemas";
import { ELSE_HANDLE, type FlowNode } from "@/features/workflows/graph";

// 분기 노드(if_else, question_classifier)는 가지마다 줄을 두고 그 줄 오른쪽 끝에 출발 핸들을 둔다.

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map((item) => (typeof item === "object" && item !== null ? item : {}))
    : [];
}

function Row({ handle, children }: { handle: string; children: ReactNode }) {
  return (
    <div className="relative border-t border-line px-3 py-1.5 text-xs">
      <div className="truncate pr-2">{children}</div>
      <Handle type="source" position={Position.Right} id={handle} />
    </div>
  );
}

function IfElseRows({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  const cases = list(node.config.cases);
  return (
    <>
      {cases.map((item, index) => {
        const conditions = list(item.conditions);
        const first = conditions[0];
        const op = typeof first?.op === "string" ? first.op : "";
        const opText = (IF_ELSE_OPS as readonly string[]).includes(op)
          ? t(`forms.ifElse.ops.${OP_KEYS[op as IfElseOp]}`)
          : op;
        const value = Array.isArray(first?.value)
          ? first.value.join(", ")
          : first?.value === null || first?.value === undefined
            ? ""
            : String(first.value);
        return (
          <Row key={String(item.id ?? index)} handle={`case_${String(item.id ?? index + 1)}`}>
            <span className="font-medium text-ink">{t("card.case", { n: index + 1 })}</span>
            <span className="ml-1.5 text-ink-muted">
              {first ? (
                <>
                  <span className="font-mono">{String(first.var ?? "") || "…"}</span> {opText}{" "}
                  {value}
                  {conditions.length > 1
                    ? ` ${item.logical === "or" ? "|" : "&"} ${t("card.conditions", { count: conditions.length })}`
                    : ""}
                </>
              ) : (
                t("card.needsSetup")
              )}
            </span>
          </Row>
        );
      })}
      <Row handle={ELSE_HANDLE}>
        <span className="font-medium text-ink">{t("card.else")}</span>
      </Row>
    </>
  );
}

function ClassifierRows({ node }: { node: FlowNode }) {
  const { t } = useTranslation("workflows");
  return (
    <>
      {list(node.config.classes).map((item, index) => (
        <Row key={String(item.id ?? index)} handle={`class_${String(item.id ?? index + 1)}`}>
          <span className="text-ink">{String(item.name ?? "") || t("card.needsSetup")}</span>
        </Row>
      ))}
    </>
  );
}

export function BranchRows({ node }: { node: FlowNode }) {
  if (node.type === "if_else") return <IfElseRows node={node} />;
  if (node.type === "question_classifier") return <ClassifierRows node={node} />;
  return null;
}

export function isBranching(type: string): boolean {
  return type === "if_else" || type === "question_classifier";
}
