import { Download, Maximize2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { TOOL_OUTPUT_VIEW_LIMIT, toolOutputUrl, useToolOutput } from "@/features/logs/api";
import { formatBytes } from "@/lib/format";

function OutputBody({ runId, toolCallId }: { runId: string; toolCallId: string }) {
  const { t } = useTranslation("logs");
  const output = useToolOutput(runId, toolCallId);
  if (output.isPending) return <Spinner label={t("tool.loadingOutput")} />;
  if (output.isError) return <ErrorAlert error={output.error} title={t("tool.outputFailed")} />;
  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex justify-end">
        <CopyButton value={output.data} />
      </div>
      {/* 도구 출력은 모델·외부에서 온 글이라 텍스트 노드로만 그린다(설계 S12). */}
      <pre className="max-h-[65vh] overflow-auto rounded-md bg-term px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-term-ink">
        {output.data || t("tool.noOutput")}
      </pre>
    </div>
  );
}

// 도구 출력 전체 보기·내려받기(설계 6.2 GET /runs/{rid}/tool-calls/{tcid}/output).
export function ToolOutputActions({
  runId,
  toolCallId,
  name,
  outputBytes,
}: {
  runId: string;
  toolCallId: string;
  name: string;
  outputBytes: number | null;
}) {
  const { t } = useTranslation("logs");
  const [open, setOpen] = useState(false);
  const tooBig = outputBytes !== null && outputBytes > TOOL_OUTPUT_VIEW_LIMIT;
  return (
    <span className="flex items-center gap-1">
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button
            size="sm"
            variant="ghost"
            disabled={tooBig}
            title={tooBig ? t("tool.tooBig") : undefined}
          >
            <Maximize2 className="size-3.5" aria-hidden />
            {t("tool.viewAll")}
          </Button>
        </DialogTrigger>
        <DialogContent
          title={t("tool.outputTitle", { name })}
          description={t("tool.outputDescription", { size: formatBytes(outputBytes) })}
          className="w-[min(60rem,calc(100vw-2rem))]"
        >
          {open ? <OutputBody runId={runId} toolCallId={toolCallId} /> : null}
        </DialogContent>
      </Dialog>
      <Button asChild size="sm" variant="ghost">
        <a href={toolOutputUrl(runId, toolCallId)} download>
          <Download className="size-3.5" aria-hidden />
          {t("tool.download")}
        </a>
      </Button>
    </span>
  );
}
