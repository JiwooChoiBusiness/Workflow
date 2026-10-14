import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { PanelRight, ScrollText } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useForm, type Resolver } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link, useBlocker, useParams } from "react-router";
import { isApiError } from "@/api/http";
import type { AgentOut, AgentVersionOut, AgentWarning } from "@/api/types";
import { ContextBarActions } from "@/app/layout/contextBar";
import { ApiTab } from "@/components/builder/ApiTab";
import { ApprovalLimitsTab } from "@/components/builder/ApprovalLimitsTab";
import { EnvVarEditor } from "@/components/builder/EnvVarEditor";
import { FilesTab } from "@/components/builder/FilesTab";
import { maxOutputTokensFor } from "@/components/builder/ModelPicker";
import { PublishPanel } from "@/components/builder/PublishPanel";
import { SettingsTab } from "@/components/builder/SettingsTab";
import { ToolsTab } from "@/components/builder/ToolsTab";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { PageSpinner, Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/toast";
import {
  agentKeys,
  useAgent,
  useAgentDraft,
  usePatchAgent,
  useSaveDraft,
} from "@/features/agents/api";
import {
  agentToForm,
  formToAgentPatch,
  formToDraftUpdate,
  makeAgentFormSchema,
  type AgentForm,
} from "@/features/agents/schemas";
import { subAgentErrorPath } from "@/features/agents/subAgents";
import { useModels } from "@/features/settings/api";
import { errorMessage } from "@/lib/errors";
import { applyServerErrors } from "@/lib/serverErrors";

// 채팅(마크다운·코드 강조)은 크므로 폼을 먼저 그리고 미리보기는 따로 받는다.
const PreviewPanel = lazy(async () => ({
  default: (await import("@/components/builder/PreviewPanel")).PreviewPanel,
}));

const TABS = ["settings", "tools", "files", "env", "approval", "publish", "api"] as const;
type TabKey = (typeof TABS)[number];

// PUT draft의 오류 위치(loc)를 폼 칸으로 옮긴다. 배열 항목 하나의 오류(스킬·파일)는 목록 전체에 붙인다.
function draftErrorPath(loc: (string | number)[]): string | null {
  const [head, ...rest] = loc.map(String);
  switch (head) {
    case "model":
    case "limits":
    case "sandbox":
    case "env_vars":
      return [head, ...rest].join(".");
    case "instructions":
    case "tools":
    case "approval_mode":
    case "skill_version_ids":
    case "file_ids":
      return head;
    case "sub_agents":
      return subAgentErrorPath(rest);
    default:
      return null;
  }
}

// PATCH agent의 오류 위치를 기본 정보 칸으로 옮긴다.
function metaErrorPath(loc: (string | number)[]): string | null {
  const head = String(loc[0] ?? "");
  return ["name", "slug", "icon", "description"].includes(head) ? `meta.${head}` : null;
}

function AgentBuilder({ agent, draft }: { agent: AgentOut; draft: AgentVersionOut }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const queryClient = useQueryClient();
  const models = useModels();
  const saveDraft = useSaveDraft(agent.id);
  const patchAgent = usePatchAgent(agent.id);
  const [tab, setTab] = useState<TabKey>("settings");
  const [previewOpen, setPreviewOpen] = useState(true);
  const [conflict, setConflict] = useState(false);
  // 마지막 저장 응답의 하위 에이전트 경고(설명 없음, 미발행, 사슬 깊이). 도구 탭의 에이전트 묶음에 보인다.
  const [warnings, setWarnings] = useState<AgentWarning[]>([]);

  const modelEntries = models.data?.models;
  // 출력 상한은 고른 모델의 능력표에 따라 달라서 검증할 때마다 스키마를 만든다.
  const resolver = useCallback<Resolver<AgentForm>>(
    (values, context, options) => {
      const schema = makeAgentFormSchema(tc, {
        maxOutputTokens: maxOutputTokensFor(modelEntries, values.model.model_id),
      });
      return zodResolver(schema)(values, context, options);
    },
    [tc, modelEntries],
  );
  const form = useForm<AgentForm>({ defaultValues: agentToForm(agent, draft), resolver });
  const { errors, isDirty, isSubmitting } = form.formState;
  // 미리보기는 저장된 초안으로 돈다. 칩도 저장된 승인 모드를 보여 준다.
  const savedDraft = useAgentDraft(agent.id).data ?? draft;

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!isDirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [isDirty]);

  function showSaveError(error: unknown, toPath: (loc: (string | number)[]) => string | null) {
    if (isApiError(error, 409) && error.code === "draft_conflict") {
      setConflict(true);
      return;
    }
    if (isApiError(error, 409) && error.code === "slug_taken") {
      form.setError("meta.slug", { type: "server", message: error.message });
      setTab("settings");
      return;
    }
    const leftover = applyServerErrors(error, form.setError, toPath);
    if (isApiError(error, 422)) {
      toast.error([t("builder.invalid"), ...leftover].join(" "));
      return;
    }
    toast.error(errorMessage(error, tc));
  }

  const save = form.handleSubmit(
    async (values) => {
      setConflict(false);
      let latestAgent = agent;
      if (form.formState.dirtyFields.meta) {
        try {
          latestAgent = await patchAgent.mutateAsync(formToAgentPatch(values.meta));
        } catch (error) {
          showSaveError(error, metaErrorPath);
          return;
        }
      }
      try {
        // 저장이 성공하면 useSaveDraft가 캐시의 초안(updated_at 포함)을 바꾼다.
        const saved = await saveDraft.mutateAsync(formToDraftUpdate(values, draft.updated_at));
        form.reset(agentToForm(latestAgent, saved));
        toast.success(t("builder.saved"));
        const found = saved.warnings ?? [];
        setWarnings(found);
        if (found.length > 0) {
          toast.warning(
            t("subAgents.saveWarnings", {
              count: found.length,
              messages: found.map((warning) => warning.message).join(" / "),
            }),
          );
        }
      } catch (error) {
        showSaveError(error, draftErrorPath);
      }
    },
    () => toast.error(t("builder.invalid")),
  );

  async function reloadDraft() {
    await queryClient.refetchQueries({ queryKey: agentKeys.draft(agent.id), exact: true });
    const fresh = queryClient.getQueryData<AgentVersionOut>(agentKeys.draft(agent.id));
    if (fresh) form.reset(agentToForm(agent, fresh));
    setConflict(false);
  }

  const tabErrors: Record<TabKey, boolean> = {
    settings: Boolean(errors.meta || errors.instructions || errors.model),
    tools: Boolean(errors.tools || errors.skill_version_ids || errors.sub_agents),
    files: Boolean(errors.file_ids),
    env: Boolean(errors.env_vars),
    approval: Boolean(errors.approval_mode || errors.limits || errors.sandbox),
    publish: false,
    api: false,
  };

  return (
    <div className="flex h-full min-h-0">
      <ContextBarActions>
        {isDirty ? <Badge tone="warn">{tc("state.unsaved")}</Badge> : null}
        <Button asChild size="sm" variant="ghost">
          <Link to={`/agents/${agent.id}/logs`}>
            <ScrollText className="size-4" aria-hidden />
            {t("builder.logs")}
          </Link>
        </Button>
        <Button size="sm" variant="primary" loading={isSubmitting} onClick={() => void save()}>
          {tc("actions.save")}
        </Button>
        <Button
          size="iconSm"
          variant="ghost"
          aria-pressed={previewOpen}
          aria-label={previewOpen ? t("builder.previewClose") : t("builder.previewOpen")}
          title={previewOpen ? t("builder.previewClose") : t("builder.previewOpen")}
          onClick={() => setPreviewOpen((open) => !open)}
        >
          <PanelRight className="size-4" aria-hidden />
        </Button>
      </ContextBarActions>
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-6 py-5">
          {conflict ? (
            <Alert
              tone="warn"
              className="mb-4"
              title={t("builder.conflict")}
              action={
                <Button size="sm" onClick={() => void reloadDraft()}>
                  {t("builder.reloadDraft")}
                </Button>
              }
            />
          ) : null}
          <Tabs value={tab} onValueChange={(value) => setTab(value as TabKey)}>
            <TabsList aria-label={t("builder.tabsLabel")}>
              {TABS.map((key) => (
                <TabsTrigger key={key} value={key}>
                  {t(`builder.tabs.${key}`)}
                  {tabErrors[key] ? (
                    <span
                      className="size-1.5 rounded-full bg-danger"
                      role="img"
                      aria-label={t("builder.tabError")}
                    />
                  ) : null}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="settings" className="pt-5">
              <SettingsTab
                form={form}
                models={models.data}
                modelsError={models.error}
                onRetryModels={() => void models.refetch()}
              />
            </TabsContent>
            <TabsContent value="tools" className="pt-5">
              <ToolsTab form={form} agentId={agent.id} warnings={warnings} />
            </TabsContent>
            <TabsContent value="files" className="pt-5">
              <FilesTab form={form} agentId={agent.id} />
            </TabsContent>
            <TabsContent value="env" className="pt-5">
              <EnvVarEditor form={form} />
            </TabsContent>
            <TabsContent value="approval" className="pt-5">
              <ApprovalLimitsTab form={form} />
            </TabsContent>
            <TabsContent value="publish" className="pt-5">
              <PublishPanel agent={agent} dirty={isDirty} />
            </TabsContent>
            <TabsContent value="api" className="pt-5">
              <ApiTab agent={agent} />
            </TabsContent>
          </Tabs>
        </div>
      </div>
      {previewOpen ? (
        <Suspense
          fallback={
            <aside className="flex w-[26rem] shrink-0 items-center justify-center border-l border-line bg-surface">
              <Spinner className="size-4" label={tc("state.loading")} />
            </aside>
          }
        >
          <PreviewPanel
            agentId={agent.id}
            approvalMode={savedDraft.approval_mode}
            dirty={isDirty}
            onClose={() => setPreviewOpen(false)}
          />
        </Suspense>
      ) : null}
      <ConfirmDialog
        open={blocker.state === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.();
        }}
        title={tc("unsavedDialog.title")}
        description={tc("unsavedDialog.body")}
        confirmLabel={tc("unsavedDialog.leave")}
        onConfirm={() => blocker.proceed?.()}
      />
    </div>
  );
}

export default function AgentBuilderPage() {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const { id = "" } = useParams();
  const agent = useAgent(id);
  const draft = useAgentDraft(id);

  if (agent.isPending || draft.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (agent.isError || draft.isError) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-6">
        <ErrorAlert
          error={agent.error ?? draft.error}
          title={t("builder.loadFailed")}
          onRetry={() => {
            void agent.refetch();
            void draft.refetch();
          }}
        />
      </div>
    );
  }
  // 다른 에이전트로 옮겨 가면 폼을 새로 만든다.
  return <AgentBuilder key={draft.data.id} agent={agent.data} draft={draft.data} />;
}
