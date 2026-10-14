import { Panel, useReactFlow } from "@xyflow/react";
import { Maximize, Redo2, Undo2, ZoomIn, ZoomOut } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";

function ToolButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <Button
      size="iconSm"
      variant="ghost"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

// 캔버스 왼쪽 아래의 줌·맞춤·실행 취소(설계 7.3). 자동 정렬은 2차다.
export function CanvasToolbar() {
  const { t } = useTranslation("workflows");
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const actions = useFlowActions();
  const canUndo = useFlow((state) => state.past.length > 0);
  const canRedo = useFlow((state) => state.future.length > 0);
  return (
    <Panel position="bottom-left">
      <div
        role="toolbar"
        aria-label={t("toolbar.label")}
        className="flex items-center gap-0.5 rounded-md border border-line bg-surface p-0.5 shadow-sm"
      >
        <ToolButton label={t("toolbar.zoomOut")} onClick={() => void zoomOut()}>
          <ZoomOut className="size-4" aria-hidden />
        </ToolButton>
        <ToolButton label={t("toolbar.zoomIn")} onClick={() => void zoomIn()}>
          <ZoomIn className="size-4" aria-hidden />
        </ToolButton>
        <ToolButton
          label={t("toolbar.fit")}
          onClick={() => void fitView({ padding: 0.2, maxZoom: 1 })}
        >
          <Maximize className="size-4" aria-hidden />
        </ToolButton>
        <span className="mx-0.5 h-5 w-px bg-line" aria-hidden />
        <ToolButton label={t("toolbar.undo")} onClick={actions.undo} disabled={!canUndo}>
          <Undo2 className="size-4" aria-hidden />
        </ToolButton>
        <ToolButton label={t("toolbar.redo")} onClick={actions.redo} disabled={!canRedo}>
          <Redo2 className="size-4" aria-hidden />
        </ToolButton>
      </div>
    </Panel>
  );
}
