import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { WorkflowKind, WorkflowOut } from "@/api/types";
import {
  API_KEY_VARIABLE,
  ApiExampleItems,
  serverOrigin,
  withBody,
  type LabeledApiExample,
} from "@/components/builder/ApiExamples";
import { ApiKeyPanelView } from "@/components/builder/ApiKeyPanel";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Panel } from "@/components/ui/layout";
import { toast } from "@/components/ui/toast";
import { Switch } from "@/components/ui/toggle";
import {
  useCreateWorkflowApiKey,
  usePatchWorkflow,
  useRevokeWorkflowApiKey,
  useWorkflowApiKeys,
} from "@/features/workflows/api";
import { useFlow } from "@/features/workflows/flowStoreContext";
import { START_TYPE } from "@/features/workflows/graph";
import { errorMessage } from "@/lib/errors";

function sampleValue(type: unknown): unknown {
  switch (type) {
    case "number":
      return 1;
    case "boolean":
      return true;
    case "file":
      return "<wf_file_id>";
    case "file_list":
      return [];
    default:
      return "…";
  }
}

// 시작 노드 필드로 만든 서비스 API 예시(설계 6.3). 키를 만들기 전에는 셸 변수 $API_KEY로 둔다.
export function useWorkflowExamples(kind: WorkflowKind, apiKey?: string): LabeledApiExample[] {
  const { t } = useTranslation("workflows");
  const fields = useFlow((state) => {
    const start = state.graph.nodes.find((node) => node.type === START_TYPE);
    return start?.config.fields;
  });
  const inputs: Record<string, unknown> = {};
  if (Array.isArray(fields)) {
    for (const field of fields as { name?: unknown; type?: unknown }[]) {
      if (typeof field.name === "string" && field.name)
        inputs[field.name] = sampleValue(field.type);
    }
  }
  const origin = serverOrigin();
  const auth = `  -H "Authorization: Bearer ${apiKey ?? API_KEY_VARIABLE}"`;
  const json = '  -H "Content-Type: application/json" \\';
  const post = (path: string, stream: boolean, body: unknown) =>
    withBody([`curl${stream ? " -N" : ""} ${origin}${path} \\`, `${auth} \\`, json], body);
  const item = (
    id: "run" | "runBlocking" | "runStatus" | "chat" | "chatBlocking",
    command: string,
  ) => ({
    id,
    title: t(`api.examples.${id}.title`),
    hint: t(`api.examples.${id}.hint`),
    command,
  });
  if (kind === "chatflow") {
    return [
      item(
        "chat",
        post("/api/v1/chat-messages", true, {
          query: "안녕하세요",
          inputs,
          user: "me",
          response_mode: "streaming",
        }),
      ),
      item(
        "chatBlocking",
        post("/api/v1/chat-messages", false, {
          query: "안녕하세요",
          inputs,
          user: "me",
          response_mode: "blocking",
        }),
      ),
    ];
  }
  return [
    item(
      "run",
      post("/api/v1/workflows/run", true, { inputs, user: "me", response_mode: "streaming" }),
    ),
    item(
      "runBlocking",
      post("/api/v1/workflows/run", false, { inputs, user: "me", response_mode: "blocking" }),
    ),
    item("runStatus", [`curl ${origin}/api/v1/workflows/run/<run_id> \\`, auth].join("\n")),
  ];
}

function Examples({ kind, apiKey }: { kind: WorkflowKind; apiKey?: string }) {
  return <ApiExampleItems examples={useWorkflowExamples(kind, apiKey)} />;
}

function WebAppToggle({ workflow }: { workflow: WorkflowOut }) {
  const { t } = useTranslation("workflows");
  const { t: ta } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const patch = usePatchWorkflow(workflow.id);
  const published = Boolean(workflow.published_version_id);
  const url = `${serverOrigin()}/flows/${encodeURIComponent(workflow.slug)}`;
  return (
    <Panel title={t("webapp.publishTitle")} description={t("webapp.publishDescription")}>
      {!published ? (
        <p className="text-sm text-ink-muted">{ta("webapp.publishFirst")}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <Switch
              id="wf-webapp-enabled"
              checked={workflow.webapp_enabled}
              disabled={patch.isPending}
              onCheckedChange={(on) =>
                patch.mutate(
                  { webapp_enabled: on },
                  {
                    onSuccess: () =>
                      toast.success(on ? ta("webapp.enabled") : ta("webapp.disabled")),
                    onError: (error) => toast.error(errorMessage(error, tc)),
                  },
                )
              }
            />
            <label htmlFor="wf-webapp-enabled" className="text-sm font-medium text-ink">
              {ta("webapp.enable")}
            </label>
          </div>
          {workflow.webapp_enabled ? (
            <div className="flex flex-wrap items-center gap-2">
              <code className="min-w-0 flex-1 rounded-md bg-code px-2.5 py-1.5 font-mono text-sm break-all text-code-ink">
                {url}
              </code>
              <CopyButton value={url} label={ta("webapp.copy")} />
              <Button asChild size="sm">
                <a
                  href={`/flows/${encodeURIComponent(workflow.slug)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink className="size-3.5" aria-hidden />
                  {t("webapp.open")}
                </a>
              </Button>
            </div>
          ) : null}
          {workflow.kind === "chatflow" ? (
            <p className="text-xs text-ink-muted">{t("webapp.chatNotReady")}</p>
          ) : null}
        </div>
      )}
    </Panel>
  );
}

// 오른쪽 패널의 API 탭: 웹앱 공개, API 키(1단계 ApiKeyPanel), 서비스 API 호출 예시.
export function WorkflowApiPanel({ workflow }: { workflow: WorkflowOut }) {
  const { t } = useTranslation("workflows");
  const { t: ta } = useTranslation("agents");
  const keys = useWorkflowApiKeys(workflow.id);
  const create = useCreateWorkflowApiKey(workflow.id);
  const revoke = useRevokeWorkflowApiKey(workflow.id);
  return (
    <div className="flex flex-col gap-4">
      {!workflow.published_version_id ? (
        <Alert tone="info" title={ta("apiKeys.notPublished")} />
      ) : null}
      <WebAppToggle workflow={workflow} />
      <ApiKeyPanelView
        source={{ keys, create: create.mutateAsync, revoke }}
        description={t("api.keysDescription")}
        renderExamples={(apiKey) => <Examples kind={workflow.kind} apiKey={apiKey} />}
      />
      <Panel title={ta("apiExamples.title")} description={t("api.examplesDescription")}>
        <div className="flex flex-col gap-3">
          <Alert tone="info" title={t("api.notReady")} />
          <Examples kind={workflow.kind} />
        </div>
      </Panel>
    </div>
  );
}
