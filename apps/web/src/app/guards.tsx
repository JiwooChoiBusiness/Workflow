import { useTranslation } from "react-i18next";
import { Navigate, Outlet, useLocation } from "react-router";
import { isApiError } from "@/api/http";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { PageSpinner } from "@/components/ui/spinner";
import { useMe } from "@/features/auth/api";

// 로그인 뒤 돌아갈 주소. 다른 사이트로 보내는 주소(//evil 등)는 받지 않는다.
export function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) {
    return "/agents";
  }
  return value;
}

export function RequireAuth() {
  const { t } = useTranslation();
  const me = useMe();
  const location = useLocation();
  if (me.isPending) return <PageSpinner label={t("state.loading")} />;
  if (me.isError) {
    if (isApiError(me.error, 401)) {
      const next = encodeURIComponent(location.pathname + location.search);
      return <Navigate to={`/login?next=${next}`} replace />;
    }
    return (
      <div className="mx-auto max-w-lg p-8">
        <ErrorAlert error={me.error} onRetry={() => void me.refetch()} />
      </div>
    );
  }
  return <Outlet />;
}
