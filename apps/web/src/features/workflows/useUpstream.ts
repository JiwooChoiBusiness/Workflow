import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { NodeTypeInfo, VarType } from "@/api/types";
import { useNodeTypes } from "./api";
import { useFlow } from "./flowStoreContext";
import {
  nodeById,
  nodeOutputs,
  sysVariables,
  upstreamIds,
  type FlowGraph,
  type VarOption,
} from "./graph";
import { typeInfoMap } from "./nodeTypes";

export interface VarGroup {
  key: string;
  // 노드면 노드 id와 타입, 예약 범위면 sys·env·conv
  scope: string;
  nodeType: string | null;
  title: string;
  vars: VarOption[];
}

export type PickerScope = "upstream" | "children";

function nodeGroups(graph: FlowGraph, ids: string[], infos: Map<string, NodeTypeInfo>): VarGroup[] {
  return ids.flatMap((id) => {
    const node = nodeById(graph, id);
    if (!node) return [];
    const vars = nodeOutputs(node, infos.get(node.type));
    if (vars.length === 0) return [];
    return [{ key: id, scope: id, nodeType: node.type, title: node.title || id, vars }];
  });
}

/*
 * 변수 선택기의 목록. upstream은 이 노드가 참조할 수 있는 상류 노드 출력과 sys·env·conv, children은
 * iteration의 output_var처럼 하위 그래프 노드의 출력만이다.
 */
export function useVariableGroups(nodeId: string, scope: PickerScope = "upstream"): VarGroup[] {
  const { t } = useTranslation("workflows");
  const graph = useFlow((state) => state.graph);
  const kind = useFlow((state) => state.kind);
  const envVars = useFlow((state) => state.envVars);
  const conversationVars = useFlow((state) => state.conversationVars);
  const nodeTypes = useNodeTypes();
  return useMemo(() => {
    const infos = typeInfoMap(nodeTypes.data);
    if (scope === "children") {
      const children = graph.nodes
        .filter((node) => node.parent_id === nodeId)
        .map((node) => node.id);
      return nodeGroups(graph, children, infos);
    }
    const groups = nodeGroups(graph, upstreamIds(graph, nodeId), infos);
    groups.push({
      key: "sys",
      scope: "sys",
      nodeType: null,
      title: t("picker.sys"),
      vars: sysVariables(kind),
    });
    const env = envVars
      .filter((row) => row.name.trim())
      .map((row) => ({
        selector: `env.${row.name.trim()}`,
        name: row.name.trim(),
        type: "string" as VarType,
      }));
    if (env.length > 0) {
      groups.push({ key: "env", scope: "env", nodeType: null, title: t("picker.env"), vars: env });
    }
    if (kind === "chatflow" && conversationVars.length > 0) {
      groups.push({
        key: "conv",
        scope: "conv",
        nodeType: null,
        title: t("picker.conv"),
        vars: conversationVars.map((item) => ({
          selector: `conv.${item.name}`,
          name: item.name,
          type: item.type,
        })),
      });
    }
    return groups;
  }, [graph, kind, envVars, conversationVars, nodeTypes.data, nodeId, scope, t]);
}

// 선택자가 가리키는 변수의 선언 타입. 하위 경로가 있거나 모르는 변수면 null이다.
export function selectorType(groups: VarGroup[], selector: string): VarType | null {
  const text = selector.trim();
  for (const group of groups) {
    const found = group.vars.find((item) => item.selector === text);
    if (found) return found.type;
  }
  return null;
}
