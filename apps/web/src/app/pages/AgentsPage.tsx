import { zodResolver } from "@hookform/resolvers/zod";
import { Bot, Ellipsis, Plus, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import type { AgentOut } from "@/api/types";
import { AgentIcon } from "@/components/builder/AgentIcon";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input, Textarea } from "@/components/ui/input";
import { EmptyState, PageHeader } from "@/components/ui/layout";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useAgents, useCreateAgent, useDeleteAgent } from "@/features/agents/api";
import { makeAgentCreateSchema, type AgentCreateForm } from "@/features/agents/schemas";
import { errorMessage } from "@/lib/errors";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";

function CreateAgentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const create = useCreateAgent();
  const navigate = useNavigate();
  const schema = useMemo(() => makeAgentCreateSchema(tc), [tc]);
  const form = useForm<AgentCreateForm>({
    resolver: zodResolver(schema),
    defaultValues: { name: "", icon: "", slug: "", description: "" },
  });
  const { errors, isSubmitting } = form.formState;

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const agent = await create.mutateAsync({
        name: values.name,
        icon: values.icon || null,
        slug: values.slug || null,
        description: values.description,
      });
      onOpenChange(false);
      form.reset();
      navigate(`/agents/${agent.id}`);
    } catch (error) {
      form.setError("root", { message: errorMessage(error, tc) });
    }
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t("create.title")}>
        <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
          <Field label={t("create.name")} error={errors.name?.message} required>
            <Input {...form.register("name")} />
          </Field>
          <div className="grid grid-cols-[6rem_1fr] gap-3">
            <Field label={t("create.icon")} error={errors.icon?.message}>
              <Input {...form.register("icon")} />
            </Field>
            <Field label={t("create.slug")} error={errors.slug?.message}>
              <Input className="font-mono" {...form.register("slug")} />
            </Field>
          </div>
          <p className="-mt-2 text-xs text-ink-muted">
            {t("create.iconHint")} {t("create.slugHint")}
          </p>
          <Field label={t("create.description")} error={errors.description?.message}>
            <Textarea rows={3} {...form.register("description")} />
          </Field>
          {errors.root?.message ? <Alert tone="danger" title={errors.root.message} /> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              {tc("actions.cancel")}
            </Button>
            <Button type="submit" variant="primary" loading={isSubmitting}>
              {t("create.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function AgentCard({ agent, onDelete }: { agent: AgentOut; onDelete: (agent: AgentOut) => void }) {
  const { t } = useTranslation("agents");
  const now = useNow();
  const published = Boolean(agent.published_version_id);
  return (
    <li className="relative flex flex-col gap-3 rounded-lg border border-line bg-surface p-4 transition-colors hover:border-line-strong">
      <div className="flex items-start gap-3">
        <AgentIcon icon={agent.icon} name={agent.name} />
        <div className="min-w-0 flex-1">
          <Link
            to={`/agents/${agent.id}`}
            className="block truncate font-medium text-ink after:absolute after:inset-0 after:rounded-lg"
          >
            {agent.name}
          </Link>
          <p className="mt-0.5 line-clamp-2 min-h-10 text-sm text-ink-muted">
            {agent.description || " "}
          </p>
        </div>
        <Menu>
          <MenuTrigger asChild>
            <Button
              variant="ghost"
              size="iconSm"
              className="relative z-10"
              aria-label={t("list.menu", { name: agent.name })}
            >
              <Ellipsis className="size-4" aria-hidden />
            </Button>
          </MenuTrigger>
          <MenuContent align="end">
            <MenuItem tone="danger" onSelect={() => onDelete(agent)}>
              <Trash2 className="size-4" aria-hidden />
              {t("list.deleteTitle")}
            </MenuItem>
          </MenuContent>
        </Menu>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-subtle">
        {published ? (
          <Badge tone="accent">
            {agent.published_version_number
              ? t("list.publishedVersion", { version: agent.published_version_number })
              : t("list.published")}
          </Badge>
        ) : (
          <Badge>{t("list.draft")}</Badge>
        )}
        <span className="font-mono">{agent.slug}</span>
        <span className="ml-auto">
          {t("list.updated", { time: formatRelative(agent.updated_at, now) })}
        </span>
      </div>
    </li>
  );
}

export default function AgentsPage() {
  const { t } = useTranslation("agents");
  const { t: tc } = useTranslation();
  const agents = useAgents();
  const remove = useDeleteAgent();
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<AgentOut | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = agents.data ?? [];
    if (!needle) return list;
    return list.filter((agent) =>
      `${agent.name} ${agent.description} ${agent.slug}`.toLowerCase().includes(needle),
    );
  }, [agents.data, query]);

  const newButton = (
    <Button variant="primary" onClick={() => setCreating(true)}>
      <Plus className="size-4" aria-hidden />
      {t("list.new")}
    </Button>
  );

  let body;
  if (agents.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (agents.isError) {
    body = (
      <ErrorAlert
        error={agents.error}
        title={t("list.loadFailed")}
        onRetry={() => void agents.refetch()}
      />
    );
  } else if (agents.data.length === 0) {
    body = (
      <EmptyState
        icon={Bot}
        title={t("list.empty")}
        hint={t("list.emptyHint")}
        action={newButton}
      />
    );
  } else if (filtered.length === 0) {
    body = <EmptyState icon={Search} title={t("list.noMatch", { query })} />;
  } else {
    body = (
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] gap-3">
        {filtered.map((agent) => (
          <AgentCard key={agent.id} agent={agent} onDelete={setDeleting} />
        ))}
      </ul>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <PageHeader
        title={t("list.title")}
        description={t("list.description")}
        actions={
          <>
            <div className="relative">
              <Search
                className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-subtle"
                aria-hidden
              />
              <Input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("list.search")}
                aria-label={t("list.search")}
                className="w-64 pl-8"
              />
            </div>
            {newButton}
          </>
        }
      />
      {body}
      <CreateAgentDialog open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={t("list.deleteTitle")}
        description={t("list.deleteBody", { name: deleting?.name ?? "" })}
        confirmLabel={tc("actions.delete")}
        pending={remove.isPending}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.success(t("list.deleted"));
              setDeleting(null);
            },
            onError: (error) => toast.error(errorMessage(error, tc)),
          });
        }}
      />
    </div>
  );
}
