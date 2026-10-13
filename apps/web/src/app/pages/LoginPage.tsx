import { zodResolver } from "@hookform/resolvers/zod";
import { useMemo } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router";
import { ApiError } from "@/api/http";
import { safeNext } from "@/app/guards";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { useLogin, useMe, useSetupStatus } from "@/features/auth/api";
import { makeLoginSchema, type LoginForm } from "@/features/auth/schemas";
import { errorMessage } from "@/lib/errors";
import { AuthCard } from "./AuthCard";

interface LoginLocationState {
  created?: boolean;
  email?: string;
}

export default function LoginPage() {
  const { t } = useTranslation("auth");
  const { t: tc } = useTranslation();
  const setup = useSetupStatus();
  const me = useMe();
  const login = useLogin();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const location = useLocation();
  const state = (location.state ?? null) as LoginLocationState | null;
  const next = safeNext(params.get("next"));
  const schema = useMemo(() => makeLoginSchema(tc), [tc]);
  const form = useForm<LoginForm>({
    resolver: zodResolver(schema),
    defaultValues: { email: state?.email ?? "", password: "" },
  });
  const { errors, isSubmitting } = form.formState;

  if (setup.data?.needs_setup) return <Navigate to="/setup" replace />;
  if (me.data) return <Navigate to={next} replace />;

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await login.mutateAsync(values);
      navigate(next, { replace: true });
    } catch (error) {
      const message =
        error instanceof ApiError && error.status === 429 && error.retryAfter
          ? t("login.rateLimited", { seconds: error.retryAfter })
          : errorMessage(error, tc);
      form.setError("root", { message });
    }
  });

  return (
    <AuthCard title={t("login.title")}>
      <form className="flex flex-col gap-4" onSubmit={onSubmit} noValidate>
        {state?.created ? <Alert tone="info" title={t("setup.createdPleaseLogin")} /> : null}
        <Field label={t("login.email")} error={errors.email?.message} required>
          <Input type="email" autoComplete="username" {...form.register("email")} />
        </Field>
        <Field label={t("login.password")} error={errors.password?.message} required>
          <Input type="password" autoComplete="current-password" {...form.register("password")} />
        </Field>
        {errors.root?.message ? <Alert tone="danger" title={errors.root.message} /> : null}
        <Button type="submit" variant="primary" loading={isSubmitting} className="justify-center">
          {t("login.submit")}
        </Button>
      </form>
    </AuthCard>
  );
}
