import { zodResolver } from "@hookform/resolvers/zod";
import { KeyRound, Plus } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { AgentOut, ApiKeyCreate, ApiKeyCreated, ApiKeyOut } from "@/api/types";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { ConfirmDialog, Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { EmptyState, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useApiKeys, useCreateApiKey, useRevokeApiKey } from "@/features/agents/api";
import { makeApiKeyCreateSchema, type ApiKeyCreateForm } from "@/features/agents/schemas";
import { errorMessage, isMissingEndpoint } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { ApiExampleList } from "./ApiExamples";

// 에이전트와 워크플로우가 같은 키 목록 화면을 쓴다. 대상마다 다른 것은 API 호출과 예시뿐이다.
export interface ApiKeySource {
  keys: {
    data: ApiKeyOut[] | undefined;
    isPending: boolean;
    isError: boolean;
    error: unknown;
    refetch: () => unknown;
  };
  create: (body: ApiKeyCreate) => Promise<ApiKeyCreated>;
  revoke: {
    isPending: boolean;
    mutate: (
      keyId: string,
      options: { onSuccess: () => void; onError: (error: unknown) => void },
    ) => void;
  };
}

function CreateKeyDialog({
  create,
  open,
  onOpenChange,
  onCreated,
}: {
  create: ApiKeySource["create"];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (key: ApiKeyCreated) => void;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const schema = useMemo(() => makeApiKeyCreateSchema(tc), [tc]);
  const form = useForm<ApiKeyCreateForm>({
    resolver: zodResolver(schema),
    defaultValues: { name: "" },
  });
  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const created = await create(values);
      form.reset();
      onOpenChange(false);
      onCreated(created);
    } catch (error) {
      form.setError("root", { message: errorMessage(error, tc) });
    }
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t("apiKeys.create")}>
        <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
          <Field label={t("apiKeys.name")} error={form.formState.errors.name?.message} required>
            <Input placeholder={t("apiKeys.namePlaceholder")} {...form.register("name")} />
          </Field>
          {form.formState.errors.root?.message ? (
            <Alert tone="danger" title={form.formState.errors.root.message} />
          ) : null}
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>{tc("actions.cancel")}</Button>
            <Button type="submit" variant="primary" loading={form.formState.isSubmitting}>
              {tc("actions.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ApiKeyPanelView({
  source,
  description,
  renderExamples,
}: {
  source: ApiKeySource;
  description?: string;
  // 만든 키를 넣은 호출 예시
  renderExamples: (apiKey: string) => ReactNode;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const { keys, revoke } = source;
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);
  // 키 경로가 없는 서버(워크플로우 키 대상 변경 전)에서는 만들기를 막는다.
  const unavailable = keys.isError && isMissingEndpoint(keys.error);

  let body;
  if (keys.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (keys.isError) {
    body = (
      <ErrorAlert
        error={keys.error}
        title={t("apiKeys.loadFailed")}
        onRetry={unavailable ? undefined : () => void keys.refetch()}
      />
    );
  } else if (!keys.data || keys.data.length === 0) {
    body = <EmptyState icon={KeyRound} title={t("apiKeys.empty")} />;
  } else {
    body = (
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium">{t("apiKeys.name")}</th>
            <th className="pb-2 font-medium">{t("apiKeys.prefix")}</th>
            <th className="pb-2 font-medium">{t("apiKeys.createdAt")}</th>
            <th className="pb-2 font-medium">{t("apiKeys.lastUsed")}</th>
            <th className="pb-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {keys.data.map((key) => (
            <tr key={key.id} className={key.revoked_at ? "text-ink-subtle" : "text-ink"}>
              <td className="py-2 pr-3">{key.name}</td>
              <td className="py-2 pr-3 font-mono text-xs">{key.key_prefix}…</td>
              <td className="py-2 pr-3 text-xs">{formatDateTime(key.created_at)}</td>
              <td className="py-2 pr-3 text-xs">
                {key.last_used_at ? formatDateTime(key.last_used_at) : t("apiKeys.never")}
              </td>
              <td className="py-2 text-right">
                {key.revoked_at ? (
                  <Badge>{t("apiKeys.revoked")}</Badge>
                ) : (
                  <Button size="sm" variant="dangerGhost" onClick={() => setRevoking(key)}>
                    {t("apiKeys.revoke")}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  return (
    <>
      <Panel
        title={t("apiKeys.title")}
        description={description ?? t("apiKeys.description")}
        actions={
          <Button size="sm" disabled={unavailable} onClick={() => setCreating(true)}>
            <Plus className="size-3.5" aria-hidden />
            {t("apiKeys.create")}
          </Button>
        }
      >
        {body}
      </Panel>
      <CreateKeyDialog
        create={source.create}
        open={creating}
        onOpenChange={setCreating}
        onCreated={setCreated}
      />
      <Dialog
        open={created !== null}
        onOpenChange={(open) => {
          if (!open) setCreated(null);
        }}
      >
        <DialogContent
          title={t("apiKeys.createdTitle")}
          description={t("apiKeys.createdBody")}
          className="w-[min(40rem,calc(100vw-2rem))]"
        >
          {created ? (
            <div className="flex flex-col gap-4">
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 rounded-md bg-code px-2.5 py-1.5 font-mono text-sm break-all text-code-ink">
                  {created.key}
                </code>
                <CopyButton value={created.key} />
              </div>
              <div>
                <h3 className="mb-2 text-sm font-semibold text-ink">{t("apiKeys.example")}</h3>
                {renderExamples(created.key)}
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => {
          if (!open) setRevoking(null);
        }}
        title={t("apiKeys.revokeTitle")}
        description={t("apiKeys.revokeBody", { name: revoking?.name ?? "" })}
        confirmLabel={t("apiKeys.revoke")}
        pending={revoke.isPending}
        onConfirm={() => {
          if (!revoking) return;
          revoke.mutate(revoking.id, {
            onSuccess: () => {
              toast.success(t("apiKeys.revokedToast"));
              setRevoking(null);
            },
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
    </>
  );
}

export function ApiKeyPanel({ agent }: { agent: AgentOut }) {
  const keys = useApiKeys(agent.id);
  const create = useCreateApiKey(agent.id);
  const revoke = useRevokeApiKey(agent.id);
  return (
    <ApiKeyPanelView
      source={{ keys, create: create.mutateAsync, revoke }}
      renderExamples={(apiKey) => <ApiExampleList apiKey={apiKey} />}
    />
  );
}
