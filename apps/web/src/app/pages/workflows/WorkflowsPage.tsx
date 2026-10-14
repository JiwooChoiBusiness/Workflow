import { zodResolver } from "@hookform/resolvers/zod";
import { Ellipsis, Plus, ScrollText, Search, Trash2, Workflow } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import * as z from "zod";
import type { WorkflowKind, WorkflowOut } from "@/api/types";
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
import { RadioCards } from "@/components/ui/radio-cards";
import { Select } from "@/components/ui/select";
import { PageSpinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { useCreateWorkflow, useDeleteWorkflow, useWorkflows } from "@/features/workflows/api";
import "@/features/workflows/i18n";
import { errorMessage } from "@/lib/errors";
import { formatRelative } from "@/lib/format";
import { requiredText, SLUG_PATTERN, type T } from "@/lib/formSchemas";
import { useNow } from "@/lib/useNow";

function createSchema(t: T) {
  return z.object({
    name: requiredText(t, 100),
    kind: z.enum(["workflow", "chatflow"]),
    template: z.enum(["blank", "chat_basic"]),
    icon: z
      .string()
      .trim()
      .max(64, { error: t("validation.maxLength", { max: 64 }) }),
    slug: z
      .string()
      .trim()
      .refine((value) => value === "" || SLUG_PATTERN.test(value), { error: t("validation.slug") }),
    description: z
      .string()
      .trim()
      .max(2000, { error: t("validation.maxLength", { max: 2000 }) }),
  });
}

type CreateForm = z.infer<ReturnType<typeof createSchema>>;

function CreateWorkflowDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const create = useCreateWorkflow();
  const navigate = useNavigate();
  const schema = useMemo(() => createSchema(tc), [tc]);
  const form = useForm<CreateForm>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: "",
      kind: "workflow",
      template: "blank",
      icon: "",
      slug: "",
      description: "",
    },
  });
  const { errors, isSubmitting } = form.formState;
  const kind = useWatch({ control: form.control, name: "kind" });

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const workflow = await create.mutateAsync({
        name: values.name,
        kind: values.kind,
        // chat_basic은 챗플로우에만 있다(서버 template_kind_mismatch).
        template: values.kind === "chatflow" ? values.template : "blank",
        icon: values.icon || null,
        slug: values.slug || null,
        description: values.description,
      });
      onOpenChange(false);
      form.reset();
      navigate(`/workflows/${workflow.id}`);
    } catch (error) {
      form.setError("root", { message: errorMessage(error, tc) });
    }
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={t("create.title")} className="w-[min(34rem,calc(100vw-2rem))]">
        <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
          <Field label={t("create.name")} error={errors.name?.message} required>
            <Input {...form.register("name")} />
          </Field>
          <Field label={t("create.kind")} hint={t("create.kindFixed")}>
            <Controller
              control={form.control}
              name="kind"
              render={({ field }) => (
                <RadioCards
                  ariaLabel={t("create.kind")}
                  value={field.value}
                  onValueChange={field.onChange}
                  options={(["workflow", "chatflow"] as const).map((value) => ({
                    value,
                    label: t(`kind.${value}`),
                    description: t(`kindHint.${value}`),
                  }))}
                />
              )}
            />
          </Field>
          {kind === "chatflow" ? (
            <Field label={t("create.template")}>
              <Controller
                control={form.control}
                name="template"
                render={({ field }) => (
                  <RadioCards
                    ariaLabel={t("create.template")}
                    value={field.value}
                    onValueChange={field.onChange}
                    options={[
                      {
                        value: "blank",
                        label: t("create.templates.blank"),
                        description: t("create.templates.blankHint"),
                      },
                      {
                        value: "chat_basic",
                        label: t("create.templates.chat_basic"),
                        description: t("create.templates.chat_basicHint"),
                      },
                    ]}
                  />
                )}
              />
            </Field>
          ) : null}
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
            <Textarea rows={2} {...form.register("description")} />
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

function WorkflowCard({
  workflow,
  onDelete,
}: {
  workflow: WorkflowOut;
  onDelete: (workflow: WorkflowOut) => void;
}) {
  const { t } = useTranslation("workflows");
  const now = useNow();
  const navigate = useNavigate();
  return (
    <li className="relative flex flex-col gap-3 rounded-lg border border-line bg-surface p-4 transition-colors hover:border-line-strong">
      <div className="flex items-start gap-3">
        <AgentIcon icon={workflow.icon} name={workflow.name} />
        <div className="min-w-0 flex-1">
          <Link
            to={`/workflows/${workflow.id}`}
            className="block truncate font-medium text-ink after:absolute after:inset-0 after:rounded-lg"
          >
            {workflow.name}
          </Link>
          <p className="mt-0.5 line-clamp-2 min-h-10 text-sm text-ink-muted">
            {workflow.description || " "}
          </p>
        </div>
        <Menu>
          <MenuTrigger asChild>
            <Button
              variant="ghost"
              size="iconSm"
              className="relative z-10"
              aria-label={t("list.menu", { name: workflow.name })}
            >
              <Ellipsis className="size-4" aria-hidden />
            </Button>
          </MenuTrigger>
          <MenuContent align="end">
            <MenuItem onSelect={() => navigate(`/workflows/${workflow.id}/logs`)}>
              <ScrollText className="size-4" aria-hidden />
              {t("editor.logs")}
            </MenuItem>
            <MenuItem tone="danger" onSelect={() => onDelete(workflow)}>
              <Trash2 className="size-4" aria-hidden />
              {t("list.deleteTitle")}
            </MenuItem>
          </MenuContent>
        </Menu>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-subtle">
        <Badge tone={workflow.kind === "chatflow" ? "info" : "neutral"}>
          {t(`kind.${workflow.kind}`)}
        </Badge>
        {workflow.published_version_number ? (
          <Badge tone="accent">
            {t("list.publishedVersion", { version: workflow.published_version_number })}
          </Badge>
        ) : (
          <Badge>{t("list.draft")}</Badge>
        )}
        <span className="font-mono">{workflow.slug}</span>
        <span className="ml-auto">
          {t("list.updated", { time: formatRelative(workflow.updated_at, now) })}
        </span>
      </div>
    </li>
  );
}

type KindFilter = "all" | WorkflowKind;

// 워크플로우 목록(설계 7.2 /workflows). 가져오기는 M18이다.
export default function WorkflowsPage() {
  const { t } = useTranslation("workflows");
  const { t: tc } = useTranslation();
  const workflows = useWorkflows();
  const remove = useDeleteWorkflow();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<WorkflowOut | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (workflows.data ?? []).filter(
      (workflow) =>
        (kind === "all" || workflow.kind === kind) &&
        (!needle ||
          `${workflow.name} ${workflow.description} ${workflow.slug}`
            .toLowerCase()
            .includes(needle)),
    );
  }, [workflows.data, query, kind]);

  const newButton = (
    <Button variant="primary" onClick={() => setCreating(true)}>
      <Plus className="size-4" aria-hidden />
      {t("list.new")}
    </Button>
  );

  let body;
  if (workflows.isPending) {
    body = <PageSpinner label={tc("state.loading")} />;
  } else if (workflows.isError) {
    body = (
      <ErrorAlert
        error={workflows.error}
        title={t("list.loadFailed")}
        onRetry={() => void workflows.refetch()}
      />
    );
  } else if (workflows.data.length === 0) {
    body = (
      <EmptyState
        icon={Workflow}
        title={t("list.empty")}
        hint={t("list.emptyHint")}
        action={newButton}
      />
    );
  } else if (filtered.length === 0) {
    body = <EmptyState icon={Search} title={t("list.noMatch")} />;
  } else {
    body = (
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(18rem,1fr))] gap-3">
        {filtered.map((workflow) => (
          <WorkflowCard key={workflow.id} workflow={workflow} onDelete={setDeleting} />
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
            <Select
              aria-label={t("list.filter")}
              className="w-32"
              value={kind}
              onValueChange={(value) => setKind(value as KindFilter)}
              options={[
                { value: "all", label: t("list.all") },
                { value: "workflow", label: t("kind.workflow") },
                { value: "chatflow", label: t("kind.chatflow") },
              ]}
            />
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
      <CreateWorkflowDialog open={creating} onOpenChange={setCreating} />
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
