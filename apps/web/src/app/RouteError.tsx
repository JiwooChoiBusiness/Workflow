import { SearchX, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";

export function RouteError() {
  const { t } = useTranslation();
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFoundPage />;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="mx-auto max-w-xl p-8">
      <EmptyState
        icon={TriangleAlert}
        title={t("errors.pageTitle")}
        hint={
          <>
            <span className="block font-mono text-xs break-all">{message}</span>
            <span className="mt-2 block">{t("errors.pageHint")}</span>
          </>
        }
        action={
          <Button variant="primary" onClick={() => window.location.reload()}>
            {t("errors.reload")}
          </Button>
        }
      />
    </div>
  );
}

export function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <div className="mx-auto max-w-xl p-8">
      <EmptyState
        icon={SearchX}
        title={t("errors.notFoundTitle")}
        hint={t("errors.notFoundHint")}
        action={
          <Button asChild variant="primary">
            <Link to="/agents">{t("errors.goHome")}</Link>
          </Button>
        }
      />
    </div>
  );
}
