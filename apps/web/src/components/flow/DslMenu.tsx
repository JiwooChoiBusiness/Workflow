import { Download, FileCode2, Upload } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { WorkflowOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { Select, type SelectOption } from "@/components/ui/select";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import { useWorkflowVersions } from "@/features/workflows/api";
import { exportWorkflowDsl, saveBlob } from "@/features/workflows/dsl";
import "@/features/workflows/i18n";
import type { Autosave } from "@/features/workflows/useDraftAutosave";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { ImportDialog } from "./ImportDialog";

function ExportForm({
  workflow,
  flush,
  onClose,
}: {
  workflow: WorkflowOut;
  flush: () => Promise<boolean>;
  onClose: () => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const versions = useWorkflowVersions(workflow.id);
  const [version, setVersion] = useState("draft");
  const [secrets, setSecrets] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const options: SelectOption[] = [
    { value: "draft", label: t("dsl.export.draft"), hint: t("dsl.export.draftHint") },
  ];
  if (workflow.published_version_number !== null) {
    options.push({
      value: "published",
      label: t("dsl.export.published", { version: workflow.published_version_number }),
    });
  }
  const numbered = [...(versions.data ?? [])].sort((a, b) => b.version_number - a.version_number);
  for (const item of numbered) {
    options.push({
      value: item.id,
      label: t("dsl.export.versionN", { version: item.version_number }),
      hint: item.change_note || formatDateTime(item.published_at ?? item.created_at),
    });
  }

  async function download() {
    setError(null);
    setPending(true);
    try {
      // 초안은 서버에 저장된 것을 내보낸다. 캔버스의 마지막 변경을 먼저 저장한다.
      if (version === "draft" && !(await flush())) {
        setError(t("dsl.export.saveFailed"));
        return;
      }
      const exported = await exportWorkflowDsl(workflow, version, secrets);
      saveBlob(exported.blob, exported.filename);
      const filename = exported.filename;
      if (exported.secretsIncluded) toast.warning(t("dsl.export.secretsIncluded", { filename }));
      else toast.success(t("dsl.export.done", { filename }));
      onClose();
    } catch (failure) {
      setError(errorMessage(failure, tc));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Field label={t("dsl.export.version")}>
        <Select value={version} onValueChange={setVersion} options={options} />
      </Field>
      {versions.isError ? (
        <p className="-mt-2 text-xs text-ink-muted">{t("dsl.export.versionsFailed")}</p>
      ) : null}
      <Field inline label={t("dsl.export.secrets")} hint={t("dsl.export.secretsHint")}>
        <Switch checked={secrets} onCheckedChange={setSecrets} />
      </Field>
      {secrets ? <Alert tone="warn" title={t("dsl.export.secretsWarning")} /> : null}
      {error ? <Alert tone="danger" title={error} /> : null}
      <DialogFooter className="mt-1">
        <Button variant="secondary" onClick={onClose}>
          {tc("actions.cancel")}
        </Button>
        <Button variant="primary" loading={pending} onClick={() => void download()}>
          <Download className="size-4" aria-hidden />
          {t("dsl.export.submit")}
        </Button>
      </DialogFooter>
    </div>
  );
}

// 편집기 상단 바의 DSL 메뉴(설계 5장). YAML로 내보내기와, 가져와서 이 초안 덮어쓰기.
export function DslMenu({
  workflow,
  autosave,
}: {
  workflow: WorkflowOut;
  autosave: Pick<Autosave, "flush" | "reload">;
}) {
  const { t } = useTranslation("workflows");
  const [open, setOpen] = useState<"export" | "import" | null>(null);
  const onOpenChange = (next: boolean) => {
    if (!next) setOpen(null);
  };
  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <Button
            size="sm"
            variant="ghost"
            aria-label={t("dsl.menu.aria")}
            title={t("dsl.menu.aria")}
          >
            <FileCode2 className="size-3.5" aria-hidden />
            {t("dsl.menu.label")}
          </Button>
        </MenuTrigger>
        <MenuContent align="end">
          <MenuItem onSelect={() => setOpen("export")}>
            <Download className="size-4" aria-hidden />
            {t("dsl.menu.export")}
          </MenuItem>
          <MenuItem onSelect={() => setOpen("import")}>
            <Upload className="size-4" aria-hidden />
            {t("dsl.menu.importInto")}
          </MenuItem>
        </MenuContent>
      </Menu>
      <Dialog open={open === "export"} onOpenChange={onOpenChange}>
        <DialogContent title={t("dsl.export.title")} description={t("dsl.export.description")}>
          <ExportForm workflow={workflow} flush={autosave.flush} onClose={() => setOpen(null)} />
        </DialogContent>
      </Dialog>
      <ImportDialog
        open={open === "import"}
        onOpenChange={onOpenChange}
        target={{
          workflowId: workflow.id,
          kind: workflow.kind,
          beforeImport: autosave.flush,
          afterImport: autosave.reload,
        }}
      />
    </>
  );
}
