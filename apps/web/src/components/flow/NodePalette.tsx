import { useReactFlow } from "@xyflow/react";
import { PanelLeftClose, PanelLeftOpen, Search } from "lucide-react";
import { useMemo, useState, type DragEvent } from "react";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useNodeTypes } from "@/features/workflows/api";
import { useFlow, useFlowStoreApi } from "@/features/workflows/flowStoreContext";
import {
  ITERATION_TYPE,
  nodeById,
  OUT_HANDLE,
  sourceHandles,
  START_TYPE,
} from "@/features/workflows/graph";
import {
  CATEGORY_ORDER,
  nodeMeta,
  paletteTypes,
  TONE_CLASS,
  typeInfoMap,
} from "@/features/workflows/nodeTypes";
import { cn } from "@/lib/cn";
import { NODE_DRAG_TYPE } from "./FlowCanvas";
import { nodeDescription, nodeLabel } from "./labels";
import { useAddNode } from "./useAddNode";

const CARD_WIDTH = 240;
const GAP = 80;

export function NodePalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("workflows");
  const kind = useFlow((state) => state.kind);
  const hasStart = useFlow((state) => state.graph.nodes.some((node) => node.type === START_TYPE));
  const nodeTypes = useNodeTypes();
  const store = useFlowStoreApi();
  const addNode = useAddNode();
  const { screenToFlowPosition } = useReactFlow();
  const [query, setQuery] = useState("");

  const infos = useMemo(() => typeInfoMap(nodeTypes.data), [nodeTypes.data]);
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const types = paletteTypes(nodeTypes.data, kind).filter((type) => {
      if (!needle) return true;
      return `${type} ${nodeLabel(t, type)} ${nodeDescription(t, type)}`
        .toLowerCase()
        .includes(needle);
    });
    return CATEGORY_ORDER.map((category) => ({
      category,
      types: types.filter((type) => nodeMeta(type).category === category),
    })).filter((group) => group.types.length > 0);
  }, [nodeTypes.data, kind, query, t]);

  // 고른 노드가 하나면 그 오른쪽에 두고 기본 출력(out)에서 이어 준다. 아니면 화면 가운데에 둔다.
  function add(type: string) {
    const state = store.getState();
    const selectedId = state.selection.length === 1 ? state.selection[0] : undefined;
    const selected = selectedId ? nodeById(state.graph, selectedId) : undefined;
    if (selected && !(type === ITERATION_TYPE && selected.parent_id)) {
      const width = state.dimensions[selected.id]?.width ?? CARD_WIDTH;
      const position = { x: selected.position.x + width + GAP, y: selected.position.y };
      const connect = sourceHandles(selected).includes(OUT_HANDLE) && type !== START_TYPE;
      addNode(type, position, {
        parentId: selected.parent_id,
        connectFrom: connect ? { source: selected.id, handle: OUT_HANDLE } : undefined,
      });
      return;
    }
    const pane = document.querySelector(".wf-flow")?.getBoundingClientRect();
    const center = pane
      ? screenToFlowPosition({ x: pane.left + pane.width / 2, y: pane.top + pane.height / 2 })
      : { x: 0, y: 0 };
    addNode(type, { x: center.x - CARD_WIDTH / 2, y: center.y - 40 });
  }

  function onDragStart(event: DragEvent, type: string) {
    event.dataTransfer.setData(NODE_DRAG_TYPE, type);
    event.dataTransfer.effectAllowed = "move";
  }

  if (!open) {
    return (
      <div className="flex w-10 shrink-0 flex-col items-center border-r border-line bg-surface py-2">
        <Button
          size="iconSm"
          variant="ghost"
          aria-label={t("palette.expand")}
          title={t("palette.expand")}
          onClick={() => onOpenChange(true)}
        >
          <PanelLeftOpen className="size-4" aria-hidden />
        </Button>
      </div>
    );
  }

  let body;
  if (nodeTypes.isPending) {
    body = (
      <div className="flex justify-center py-6 text-ink-muted">
        <Spinner />
      </div>
    );
  } else if (nodeTypes.isError) {
    body = (
      <ErrorAlert
        error={nodeTypes.error}
        title={t("palette.loadFailed")}
        onRetry={() => void nodeTypes.refetch()}
      />
    );
  } else if (groups.length === 0) {
    body = <p className="px-1 py-4 text-center text-sm text-ink-muted">{t("palette.noMatch")}</p>;
  } else {
    body = groups.map((group) => (
      <section key={group.category} className="flex flex-col gap-1">
        <h3 className="px-1 text-xs font-medium text-ink-subtle">
          {t(`categories.${group.category}`)}
        </h3>
        <ul className="flex flex-col gap-0.5">
          {group.types.map((type) => {
            const meta = nodeMeta(type);
            const Icon = meta.icon;
            const label = nodeLabel(t, type);
            const implemented = infos.get(type)?.implemented ?? true;
            const blocked = type === START_TYPE && hasStart;
            return (
              <li key={type}>
                <button
                  type="button"
                  draggable={!blocked}
                  disabled={blocked}
                  onDragStart={(event) => onDragStart(event, type)}
                  onClick={() => add(type)}
                  aria-label={t("palette.add", { name: label })}
                  title={blocked ? t("palette.startExists") : nodeDescription(t, type)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-sm text-ink",
                    "hover:bg-hover disabled:cursor-not-allowed disabled:opacity-50",
                  )}
                >
                  <span
                    className={cn(
                      "grid size-6 shrink-0 place-items-center rounded-md",
                      TONE_CLASS[meta.tone],
                    )}
                  >
                    <Icon className="size-3.5" aria-hidden />
                  </span>
                  <span className="min-w-0 flex-1 truncate">{label}</span>
                  {!implemented ? (
                    <Badge tone="warn" title={t("palette.notImplementedHint")}>
                      {t("palette.notImplemented")}
                    </Badge>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ul>
      </section>
    ));
  }

  return (
    <aside
      aria-label={t("palette.title")}
      className="flex w-56 shrink-0 flex-col border-r border-line bg-surface"
    >
      <div className="flex items-center justify-between gap-2 px-3 pt-3 pb-2">
        <h2 className="text-sm font-semibold text-ink">{t("palette.title")}</h2>
        <Button
          size="iconSm"
          variant="ghost"
          aria-label={t("palette.collapse")}
          title={t("palette.collapse")}
          onClick={() => onOpenChange(false)}
        >
          <PanelLeftClose className="size-4" aria-hidden />
        </Button>
      </div>
      <div className="relative px-3 pb-2">
        <Search
          className="pointer-events-none absolute top-2 left-5 size-4 text-ink-subtle"
          aria-hidden
        />
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("palette.search")}
          aria-label={t("palette.search")}
          className="pl-8"
        />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 pb-3">{body}</div>
      <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">{t("palette.hint")}</p>
    </aside>
  );
}
