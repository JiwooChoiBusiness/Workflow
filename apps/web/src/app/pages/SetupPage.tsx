import { zodResolver } from "@hookform/resolvers/zod";
import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { PageSpinner } from "@/components/ui/spinner";
import { useLogin, useSetup, useSetupStatus } from "@/features/auth/api";
import { makeSetupSchema, type SetupForm } from "@/features/auth/schemas";
import { errorMessage } from "@/lib/errors";
import { AuthCard } from "./AuthCard";

const FALLBACK_MIN_LENGTH = 12;

export default function SetupPage() {
  const { t } = useTranslation("auth");
  const { t: tc } = useTranslation();
  const status = useSetupStatus();
  const setup = useSetup();
  const login = useLogin();
  const navigate = useNavigate();
  // 계정을 만든 직후에는 설정 상태가 "끝남"으로 바뀌어도 안내 대신 진행 표시를 보인다.
  const [completing, setCompleting] = useState(false);
  const minLength = status.data?.password_min_length || FALLBACK_MIN_LENGTH;
  const schema = useMemo(() => makeSetupSchema(tc, minLength), [tc, minLength]);
  const form = useForm<SetupForm>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", name: "", workspace_name: "", password: "", password_confirm: "" },
  });
  const { errors, isSubmitting } = form.formState;

  if (status.isPending || completing) return <PageSpinner label={tc("state.loading")} />;
  if (status.isError) {
    return (
      <AuthCard title={t("setup.title")}>
        <ErrorAlert error={status.error} onRetry={() => void status.refetch()} />
      </AuthCard>
    );
  }
  if (!status.data.needs_setup) {
    return (
      <AuthCard title={t("setup.title")}>
        <p className="text-sm text-ink-muted">{t("setup.alreadyDone")}</p>
        <Button asChild variant="primary" className="mt-4 w-full justify-center">
          <Link to="/login">{t("setup.goLogin")}</Link>
        </Button>
      </AuthCard>
    );
  }

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await setup.mutateAsync({
        email: values.email,
        password: values.password,
        name: values.name || null,
        workspace_name: values.workspace_name || null,
      });
    } catch (error) {
      form.setError("root", { message: errorMessage(error, tc) });
      return;
    }
    setCompleting(true);
    // 설정 API는 로그인까지 해 주지 않는다. 방금 입력한 값으로 바로 로그인한다.
    try {
      await login.mutateAsync({ email: values.email, password: values.password });
      navigate("/agents", { replace: true });
    } catch {
      navigate("/login", { replace: true, state: { created: true, email: values.email } });
    }
  });

  return (
    <AuthCard title={t("setup.title")} description={t("setup.description")}>
      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        <Field label={t("setup.email")} error={errors.email?.message} required>
          <Input type="email" autoComplete="username" {...form.register("email")} />
        </Field>
        <Field label={t("setup.name")} error={errors.name?.message}>
          <Input placeholder={t("setup.namePlaceholder")} {...form.register("name")} />
        </Field>
        <Field label={t("setup.workspaceName")} error={errors.workspace_name?.message}>
          <Input
            placeholder={t("setup.workspacePlaceholder")}
            {...form.register("workspace_name")}
          />
        </Field>
        <Field
          label={t("setup.password")}
          hint={t("setup.passwordHint", { min: minLength })}
          error={errors.password?.message}
          required
        >
          <Input type="password" autoComplete="new-password" {...form.register("password")} />
        </Field>
        <Field label={t("setup.passwordConfirm")} error={errors.password_confirm?.message} required>
          <Input
            type="password"
            autoComplete="new-password"
            {...form.register("password_confirm")}
          />
        </Field>
        {errors.root?.message ? <Alert tone="danger" title={errors.root.message} /> : null}
        <Button type="submit" variant="primary" loading={isSubmitting} className="justify-center">
          {t("setup.submit")}
        </Button>
      </form>
    </AuthCard>
  );
}
