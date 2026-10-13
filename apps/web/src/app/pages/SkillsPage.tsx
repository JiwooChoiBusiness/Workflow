import { Download, Ellipsis, FileText, Puzzle, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { SkillDetailOut, SkillOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog, DialogContent } from "@/components/ui/dialog";
import { EmptyState, PageHeader } from "@/components/ui/layout";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import {
  useDeleteSkill,
  useSkill,
  useSkillFile,
  useSkills,
  useUploadSkill,
  useUploadSkillVersion,
} from "@/features/skills/api";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";

function languageFor(path: string) {
  if (path.endsWith(".md")) return "markdown" as const;
  if (path.endsWith(".json")) return "json" as const;
  return "text" as const;
}

function SkillFileView({
  skillId,
  versionId,
  path,
}: {
  skillId: string;
  versionId: string;
  path: string;
}) {
  const { t } = useTranslation("skills");
  const content = useSkillFile(skillId, versionId, path);
  if (content.isPending) return <Spinner />;
  if (content.isError) return <ErrorAlert error={content.error} title={t("fileLoadFailed")} />;
  if (content.data.kind === "binary") {
    return (
      <div className="flex flex-col items-start gap-2 text-sm text-ink-muted">
        {t("binaryFile")}
        <Button asChild size="sm">
          <a href={content.data.url} download>
            <Download className="size-3.5" aria-hidden />
            {t("download")}
          </a>
        </Button>
      </div>
    );
  }
  return (
    <CodeEditor
      value={content.data.text}
      readOnly
      language={languageFor(path)}
      ariaLabel={path}
      className="h-full"
      minHeight={200}
    />
  );
}

function SkillViewer({ detail }: { detail: SkillDetailOut }) {
  const { t } = useTranslation("skills");
  const version = detail.current_version;
  const files = version?.manifest.files ?? [];
  const [path, setPath] = useState<string | null>(
    files.find((file) => file.path === "SKILL.md")?.path ?? files[0]?.path ?? null,
  );
  if (!version) return <p className="text-sm text-ink-muted">{t("noVersion")}</p>;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <Badge>{t("version", { version: version.version_number })}</Badge>
        <span>{formatBytes(version.size_bytes)}</span>
        <span>{t("fileCount", { count: files.length })}</span>
        <span className="font-mono">{detail.slug}</span>
      </div>
      <div className="grid h-[60vh] grid-cols-[14rem_minmax(0,1fr)] gap-3">
        <nav aria-label={t("files")} className="overflow-y-auto rounded-md border border-line">
          <ul className="p-1">
            {files.map((file) => (
              <li key={file.path}>
                <button
                  type="button"
                  onClick={() => setPath(file.path)}
                  aria-current={file.path === path ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs",
                    file.path === path
                      ? "bg-accent-soft text-ink"
                      : "text-ink-muted hover:bg-hover",
                  )}
                >
                  <FileText className="size-3.5 shrink-0" aria-hidden />
                  <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  <span className="shrink-0 text-ink-subtle">{formatBytes(file.size)}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="min-h-0 overflow-hidden">
          {path ? (
            <SkillFileView skillId={detail.id} versionId={version.id} path={path} />
          ) : (
            <p className="text-sm text-ink-muted">{t("pickFile")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

function SkillDialog({ skillId, onClose }: { skillId: string | null; onClose: () => void }) {
  const { t } = useTranslation("skills");
  const { t: tc } = useTranslation();
  const detail = useSkill(skillId);
  return (
    <Dialog
      open={skillId !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        title={detail.data?.name ?? t("view")}
        description={detail.data?.description || undefined}
        className="w-[min(64rem,calc(100vw-2rem))]"
      >
        {detail.isPending ? (
          <PageSpinner label={tc("state.loading")} />
        ) : detail.isError ? (
          <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} />
        ) : (
          <SkillViewer key={detail.data.current_version_id} detail={detail.data} />
        )}
      </DialogContent>
    </Dialog>
  );
}

export default function SkillsPage() {
  const { t } = useTranslation("skills");
  const { t: tc } = useTranslation();
  const skills = useSkills();
  const upload = useUploadSkill();
  const uploadVersion = useUploadSkillVersion();
  const remove = useDeleteSkill();
  const now = useNow();
  const uploadRef = useRef<HTMLInputElement>(null);
  const versionRef = useRef<HTMLInputElement>(null);
  const [versionTarget, setVersionTarget] = useState<SkillOut | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<SkillOut | null>(null);

  const uploadButton = (
    <Button variant="primary" loading={upload.isPending} onClick={() => uploadRef.current?.click()}>
      <Upload className="size-4" aria-hidden />
      {t("upload")}
    </Button>
  );

  let body;
  if (skills.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (skills.isError) {
    body = (
      <ErrorAlert
        error={skills.error}
        title={t("loadFailed")}
        onRetry={() => void skills.refetch()}
      />
    );
  } else if (skills.data.length === 0) {
    body = (
      <EmptyState icon={Puzzle} title={t("empty")} hint={t("emptyHint")} action={uploadButton} />
    );
  } else {
    body = (
      <ul className="divide-y divide-line rounded-lg border border-line bg-surface">
        {skills.data.map((skill) => (
          <li key={skill.id} className="flex items-start gap-3 px-4 py-3">
            <Puzzle className="mt-0.5 size-5 shrink-0 text-accent" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{skill.name}</span>
                <span className="font-mono text-xs text-ink-subtle">{skill.slug}</span>
                <Badge>
                  {skill.current_version_number
                    ? t("version", { version: skill.current_version_number })
                    : t("noVersion")}
                </Badge>
              </div>
              {skill.description ? (
                <p className="mt-0.5 line-clamp-2 text-sm text-ink-muted">{skill.description}</p>
              ) : null}
              <p className="mt-1 text-xs text-ink-subtle">
                {t("updated", { time: formatRelative(skill.updated_at, now) })}
              </p>
            </div>
            <Button
              size="sm"
              disabled={!skill.current_version_id}
              onClick={() => setViewing(skill.id)}
            >
              {t("view")}
            </Button>
            <Menu>
              <MenuTrigger asChild>
                <Button variant="ghost" size="iconSm" aria-label={t("menu", { name: skill.name })}>
                  <Ellipsis className="size-4" aria-hidden />
                </Button>
              </MenuTrigger>
              <MenuContent align="end">
                <MenuItem
                  onSelect={() => {
                    setVersionTarget(skill);
                    versionRef.current?.click();
                  }}
                >
                  <Upload className="size-4" aria-hidden />
                  {t("newVersion")}
                </MenuItem>
                <MenuItem tone="danger" onSelect={() => setDeleting(skill)}>
                  <Trash2 className="size-4" aria-hidden />
                  {t("deleteTitle")}
                </MenuItem>
              </MenuContent>
            </Menu>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <PageHeader title={t("title")} description={t("description")} actions={uploadButton} />
      <p className="-mt-4 mb-4 text-xs text-ink-muted">{t("uploadHint")}</p>
      <input
        ref={uploadRef}
        type="file"
        accept=".zip,.skill"
        className="hidden"
        aria-hidden
        tabIndex={-1}
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          upload.mutate(file, {
            onSuccess: () => toast.success(t("uploaded")),
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
      <input
        ref={versionRef}
        type="file"
        accept=".zip,.skill"
        className="hidden"
        aria-hidden
        tabIndex={-1}
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file || !versionTarget) return;
          uploadVersion.mutate(
            { skillId: versionTarget.id, file },
            {
              onSuccess: () => toast.success(t("newVersionUploaded")),
              onError: (error) => toast.error(errorMessage(error, tc)),
            },
          );
        }}
      />
      {body}
      <SkillDialog skillId={viewing} onClose={() => setViewing(null)} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("deleteTitle")}
        description={t("deleteBody", { name: deleting?.name ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
      {uploadVersion.isPending ? (
        <div className="fixed right-4 bottom-4 flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-2 text-sm shadow-md">
          <Spinner /> {t("newVersion")}
        </div>
      ) : null}
    </div>
  );
}
