import { zodResolver } from "@hookform/resolvers/zod";
import { LogOut } from "lucide-react";
import { useMemo } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { KeyValueList, PageHeader, Panel } from "@/components/ui/layout";
import { toast } from "@/components/ui/toast";
import { useChangePassword, useLogout, useMe } from "@/features/auth/api";
import { makePasswordChangeSchema, type PasswordChangeForm } from "@/features/auth/schemas";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

// 서버 MIN_PASSWORD_LENGTH(app/security/passwords.py). 설정 상태 API는 계정이 생긴 뒤 404라 여기서는 상수로 둔다.
const PASSWORD_MIN_LENGTH = 12;

function PasswordForm() {
  const { t } = useTranslation("auth");
  const { t: tc } = useTranslation();
  const change = useChangePassword();
  const schema = useMemo(() => makePasswordChangeSchema(tc, PASSWORD_MIN_LENGTH), [tc]);
  const form = useForm<PasswordChangeForm>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: "", new_password: "", new_password_confirm: "" },
  });
  const { errors, isSubmitting } = form.formState;
  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await change.mutateAsync({
        current_password: values.current_password,
        new_password: values.new_password,
      });
      form.reset();
      toast.success(t("account.changed"));
    } catch (error) {
      form.setError("root", { message: errorMessage(error, tc) });
    }
  });
  return (
    <form className="flex max-w-sm flex-col gap-4" onSubmit={onSubmit} noValidate>
      <Field label={t("account.currentPassword")} error={errors.current_password?.message} required>
        <Input
          type="password"
          autoComplete="current-password"
          {...form.register("current_password")}
        />
      </Field>
      <Field
        label={t("account.newPassword")}
        hint={t("setup.passwordHint", { min: PASSWORD_MIN_LENGTH })}
        error={errors.new_password?.message}
        required
      >
        <Input type="password" autoComplete="new-password" {...form.register("new_password")} />
      </Field>
      <Field
        label={t("account.newPasswordConfirm")}
        error={errors.new_password_confirm?.message}
        required
      >
        <Input
          type="password"
          autoComplete="new-password"
          {...form.register("new_password_confirm")}
        />
      </Field>
      {errors.root?.message ? <Alert tone="danger" title={errors.root.message} /> : null}
      <div>
        <Button type="submit" variant="primary" loading={isSubmitting}>
          {t("account.changePassword")}
        </Button>
      </div>
    </form>
  );
}

export default function AccountPage() {
  const { t } = useTranslation("auth");
  const { t: tc } = useTranslation();
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const account = me.data?.account;
  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <PageHeader title={t("account.title")} />
      <div className="flex flex-col gap-6">
        <Panel title={t("account.info")}>
          <KeyValueList
            items={[
              { label: t("account.email"), value: account?.email ?? "-" },
              { label: t("account.name"), value: account?.name ?? "-" },
              { label: t("account.workspace"), value: me.data?.workspace.name ?? "-" },
              { label: t("account.lastLogin"), value: formatDateTime(account?.last_login_at) },
              { label: t("account.createdAt"), value: formatDateTime(account?.created_at) },
            ]}
          />
        </Panel>
        <Panel title={t("account.passwordTitle")} description={t("account.passwordDescription")}>
          <PasswordForm />
        </Panel>
        <Panel title={t("account.logoutTitle")} description={t("account.logoutDescription")}>
          <Button
            loading={logout.isPending}
            onClick={() =>
              logout.mutate(undefined, {
                onSettled: (_data, error) => {
                  if (error) toast.error(errorMessage(error, tc));
                  navigate("/login", { replace: true });
                },
              })
            }
          >
            <LogOut className="size-4" aria-hidden />
            {tc("actions.logout")}
          </Button>
        </Panel>
      </div>
    </div>
  );
}
