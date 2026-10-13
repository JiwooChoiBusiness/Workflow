import { useQueries } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useRef } from "react";
import { useWatch, type UseFormReturn } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";
import { TOOL_GROUPS, type ApprovalMode, type SkillOut, type ToolGroup } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/layout";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Checkbox, Switch } from "@/components/ui/toggle";
import { useEmbeddedSkills, useUploadEmbeddedSkill } from "@/features/agents/api";
import type { AgentForm } from "@/features/agents/schemas";
import { fetchSkill, skillKeys, useSkills } from "@/features/skills/api";
import { errorMessage } from "@/lib/errors";

function approvalNote(
  group: ToolGroup,
  mode: ApprovalMode,
): "needsApproval" | "writesNeedApproval" | null {
  if (group === "shell" && mode === "all_shell") return "needsApproval";
  if (group === "http" && mode !== "off") return "writesNeedApproval";
  return null;
}

function ToolGroupToggles({ form }: { form: UseFormReturn<AgentForm> }) {
  const { t } = useTranslation("agents");
  const tools = useWatch({ control: form.control, name: "tools" });
  const mode = useWatch({ control: form.control, name: "approval_mode" });

  function toggle(group: ToolGroup, on: boolean) {
    const next = TOOL_GROUPS.filter((item) => (item === group ? on : tools.includes(item)));
    form.setValue("tools", next, { shouldDirty: true });
  }

  return (
    <ul className="divide-y divide-line">
      {TOOL_GROUPS.map((group) => {
        const id = `tool-group-${group}`;
        const note = approvalNote(group, mode);
        return (
          <li key={group} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
            <Switch
              id={id}
              checked={tools.includes(group)}
              onCheckedChange={(on) => toggle(group, on)}
              aria-describedby={`${id}-desc`}
            />
            <div className="min-w-0 flex-1">
              <label htmlFor={id} className="text-sm font-medium text-ink">
                {t(`tools.groups.${group}.label`)}
              </label>
              <p id={`${id}-desc`} className="text-xs text-ink-muted">
                {t(`tools.groups.${group}.description`)}
              </p>
            </div>
            {note ? <Badge tone="warn">{t(`tools.${note}`)}</Badge> : null}
          </li>
        );
      })}
    </ul>
  );
}

interface PinnedVersion {
  skillId: string;
  versionNumber: number;
}

function SkillRow({
  skill,
  pinned,
  onCheckedChange,
  onUseLatest,
}: {
  skill: SkillOut;
  // 이 스킬에서 초안이 고정한 버전. 현재 버전이 아닐 수도 있다.
  pinned: { id: string; versionNumber: number | null } | null;
  onCheckedChange: (checked: boolean) => void;
  onUseLatest: () => void;
}) {
  const { t } = useTranslation("agents");
  const id = `skill-${skill.id}`;
  const outdated = pinned !== null && pinned.id !== skill.current_version_id;
  return (
    <li className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
      <Checkbox
        id={id}
        className="mt-0.5"
        checked={pinned !== null}
        disabled={!skill.current_version_id && pinned === null}
        onCheckedChange={(value) => onCheckedChange(value === true)}
      />
      <div className="min-w-0 flex-1">
        <label
          htmlFor={id}
          className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink"
        >
          {skill.name}
          <span className="font-mono text-xs font-normal text-ink-subtle">{skill.slug}</span>
          {skill.current_version_number ? (
            <Badge>{t("skills.version", { version: skill.current_version_number })}</Badge>
          ) : null}
        </label>
        {skill.description ? (
          <p className="line-clamp-2 text-xs text-ink-muted">{skill.description}</p>
        ) : null}
        {outdated ? (
          <p className="mt-1 flex items-center gap-2 text-xs text-warn">
            {t("skills.pinnedOld", { version: pinned.versionNumber ?? "?" })}
            <Button size="sm" variant="ghost" onClick={onUseLatest}>
              {t("skills.useLatest")}
            </Button>
          </p>
        ) : null}
      </div>
    </li>
  );
}

function SkillPicker({ form, agentId }: { form: UseFormReturn<AgentForm>; agentId: string }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const library = useSkills();
  const embedded = useEmbeddedSkills(agentId);
  const upload = useUploadEmbeddedSkill(agentId);
  const selected = useWatch({ control: form.control, name: "skill_version_ids" });
  const inputRef = useRef<HTMLInputElement>(null);
  const listError = form.formState.errors.skill_version_ids?.message;

  const skills = [...(library.data ?? []), ...(embedded.data ?? [])];
  const current = new Set(skills.map((skill) => skill.current_version_id).filter(Boolean));
  const unresolved = selected.filter((id) => !current.has(id));
  // 현재 버전이 아닌 id가 고정되어 있을 때만 스킬 상세(버전 목록)를 읽어 어느 스킬의 몇 번째 버전인지 찾는다.
  const details = useQueries({
    queries: (unresolved.length > 0 ? skills : []).map((skill) => ({
      queryKey: skillKeys.detail(skill.id),
      queryFn: () => fetchSkill(skill.id),
      staleTime: 60_000,
    })),
  });
  const owners = new Map<string, PinnedVersion>();
  for (const { data } of details) {
    if (!data) continue;
    for (const version of data.versions) {
      owners.set(version.id, { skillId: data.id, versionNumber: version.version_number });
    }
  }
  for (const skill of skills) {
    if (skill.current_version_id) {
      owners.set(skill.current_version_id, {
        skillId: skill.id,
        versionNumber: skill.current_version_number ?? 0,
      });
    }
  }
  const resolving = details.some((detail) => detail.isPending);
  const orphans = selected.filter((id) => !owners.has(id));

  const setSelected = (ids: string[]) =>
    form.setValue("skill_version_ids", ids, { shouldDirty: true });

  function pinnedOf(skill: SkillOut) {
    const id = selected.find((value) => owners.get(value)?.skillId === skill.id);
    return id ? { id, versionNumber: owners.get(id)?.versionNumber ?? null } : null;
  }

  function toggle(skill: SkillOut, on: boolean) {
    // 같은 스킬의 버전은 하나만 둔다(서버 skill_duplicate).
    const rest = selected.filter((id) => owners.get(id)?.skillId !== skill.id);
    setSelected(on && skill.current_version_id ? [...rest, skill.current_version_id] : rest);
  }

  const renderList = (list: SkillOut[], empty: string) =>
    list.length === 0 ? (
      <p className="text-sm text-ink-muted">{empty}</p>
    ) : (
      <ul className="divide-y divide-line">
        {list.map((skill) => (
          <SkillRow
            key={skill.id}
            skill={skill}
            pinned={pinnedOf(skill)}
            onCheckedChange={(on) => toggle(skill, on)}
            onUseLatest={() => toggle(skill, true)}
          />
        ))}
      </ul>
    );

  return (
    <div className="flex flex-col gap-4">
      {listError ? <Alert tone="danger" title={listError} /> : null}
      <Panel
        title={t("skills.library")}
        description={t("skills.libraryHint")}
        actions={
          <Button asChild size="sm" variant="ghost">
            <Link to="/skills">{t("skills.manage")}</Link>
          </Button>
        }
      >
        {library.isPending ? (
          <Spinner />
        ) : library.isError ? (
          <ErrorAlert
            error={library.error}
            title={t("skills.loadFailed")}
            onRetry={() => void library.refetch()}
          />
        ) : (
          renderList(library.data, t("skills.none"))
        )}
      </Panel>
      <Panel
        title={t("skills.embedded")}
        description={t("skills.embeddedHint")}
        actions={
          <>
            <input
              ref={inputRef}
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
                  onSuccess: (skill) => {
                    // 올린 스킬은 초안에 저절로 들어가지 않는다. 고른 상태로 두고 저장하게 한다.
                    if (skill.current_version_id) {
                      setSelected([
                        ...form.getValues("skill_version_ids"),
                        skill.current_version_id,
                      ]);
                    }
                    toast.success(t("skills.uploaded"));
                  },
                  onError: (error) => toast.error(errorMessage(error, tc)),
                });
              }}
            />
            <Button size="sm" loading={upload.isPending} onClick={() => inputRef.current?.click()}>
              <Upload className="size-3.5" aria-hidden />
              {t("skills.embeddedUpload")}
            </Button>
          </>
        }
      >
        {embedded.isPending ? (
          <Spinner />
        ) : embedded.isError ? (
          <ErrorAlert
            error={embedded.error}
            title={t("skills.loadFailed")}
            onRetry={() => void embedded.refetch()}
          />
        ) : (
          renderList(embedded.data, t("skills.noneEmbedded"))
        )}
      </Panel>
      {library.isSuccess && embedded.isSuccess && !resolving && orphans.length > 0 ? (
        <div className="flex items-center gap-3 text-sm text-ink-muted">
          {t("skills.unknownPinned", { count: orphans.length })}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setSelected(selected.filter((id) => owners.has(id)))}
          >
            {t("skills.removeUnknown")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function ToolsTab({ form, agentId }: { form: UseFormReturn<AgentForm>; agentId: string }) {
  const { t } = useTranslation("agents");
  return (
    <div className="flex flex-col gap-6">
      <Panel title={t("tools.title")} description={t("tools.description")}>
        <ToolGroupToggles form={form} />
      </Panel>
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-ink">{t("skills.title")}</h2>
        <SkillPicker form={form} agentId={agentId} />
      </section>
    </div>
  );
}
