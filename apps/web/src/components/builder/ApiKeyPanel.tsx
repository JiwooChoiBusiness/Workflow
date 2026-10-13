import { zodResolver } from "@hookform/resolvers/zod";
import { KeyRound, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import type { AgentOut, ApiKeyCreated, ApiKeyOut } from "@/api/types";
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
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

function curlExample(key: string): string {
  const origin = typeof window === "undefined" ? "http://127.0.0.1:8500" : window.location.origin;
  return [
    `curl -N ${origin}/api/v1/chat-messages \\`,
    `  -H "Authorization: Bearer ${key}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"query": "안녕하세요", "response_mode": "streaming"}'`,
  ].join("\n");
}

function CreateKeyDialog({
  agentId,
  open,
  onOpenChange,
  onCreated,
}: {
  agentId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (key: ApiKeyCreated) => void;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const create = useCreateApiKey(agentId);
  const schema = useMemo(() => makeApiKeyCreateSchema(tc), [tc]);
  const form = useForm<ApiKeyCreateForm>({
    resolver: zodResolver(schema),
    defaultValues: { name: "" },
  });
  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const created = await create.mutateAsync(values);
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

export function ApiKeyPanel({ agent }: { agent: AgentOut }) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const keys = useApiKeys(agent.id);
  const revoke = useRevokeApiKey(agent.id);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);

  let body;
  if (keys.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (keys.isError) {
    body = (
      <ErrorAlert
        error={keys.error}
        title={t("apiKeys.loadFailed")}
        onRetry={() => void keys.refetch()}
      />
    );
  } else if (keys.data.length === 0) {
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
    <div className="flex flex-col gap-4">
      {!agent.published_version_id ? <Alert tone="info" title={t("apiKeys.notPublished")} /> : null}
      <Panel
        title={t("apiKeys.title")}
        description={t("apiKeys.description")}
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="size-3.5" aria-hidden />
            {t("apiKeys.create")}
          </Button>
        }
      >
        {body}
      </Panel>
      <CreateKeyDialog
        agentId={agent.id}
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
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-sm font-medium text-ink">{t("apiKeys.example")}</span>
                  <CopyButton value={curlExample(created.key)} size="iconSm" variant="ghost" />
                </div>
                <pre className="overflow-x-auto rounded-md bg-code p-3 font-mono text-xs text-code-ink">
                  {curlExample(created.key)}
                </pre>
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
    </div>
  );
}
