import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValueList, PageHeader, Panel } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useHttpAllowlist } from "@/features/settings/api";
import { formatBytes, formatDateTime } from "@/lib/format";

function Entries({ values, empty }: { values: string[]; empty: string }) {
  if (values.length === 0) return <span className="text-ink-subtle">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {values.map((value) => (
        <Badge key={value} className="font-mono">
          {value}
        </Badge>
      ))}
    </span>
  );
}

// 설정 > HTTP 허용 목록(설계 7.3 AllowlistView). 보기만 하고 고치는 것은 파일에서 한다.
export default function HttpAllowlistPage() {
  const { t } = useTranslation("settings");
  const { t: tc } = useTranslation();
  const allowlist = useHttpAllowlist();

  if (allowlist.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (allowlist.isError) {
    return (
      <ErrorAlert
        error={allowlist.error}
        title={t("allowlist.loadFailed")}
        onRetry={() => void allowlist.refetch()}
      />
    );
  }
  const data = allowlist.data;
  const rows: { label: ReactNode; value: ReactNode }[] = [
    {
      label: t("allowlist.path"),
      value: (
        <span className="flex min-w-0 items-center gap-1">
          <span className="min-w-0 font-mono text-xs break-all">{data.path}</span>
          <CopyButton value={data.path} size="iconSm" variant="ghost" />
        </span>
      ),
    },
    {
      label: t("allowlist.modified"),
      value: data.exists ? formatDateTime(data.modified_at) : "-",
    },
    { label: t("allowlist.size"), value: data.exists ? formatBytes(data.size_bytes) : "-" },
    {
      label: t("allowlist.reload"),
      value: t("allowlist.reloadValue", { seconds: data.reload_interval_sec }),
    },
  ];
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={t("allowlist.title")}
        description={t("allowlist.description")}
        className="mb-0"
        actions={
          <Button size="sm" loading={allowlist.isFetching} onClick={() => void allowlist.refetch()}>
            <RefreshCw className="size-3.5" aria-hidden />
            {t("allowlist.refresh")}
          </Button>
        }
      />
      {!data.exists ? (
        <Alert tone="warn" title={t("allowlist.missing")}>
          {t("allowlist.missingHint")}
        </Alert>
      ) : null}
      {data.rules.allow_any ? (
        <Alert tone="warn" title={t("allowlist.allowAny")}>
          {t("allowlist.allowAnyHint")}
        </Alert>
      ) : null}
      <Panel title={t("allowlist.file")}>
        <KeyValueList items={rows} />
      </Panel>
      <Panel title={t("allowlist.rules")} description={t("allowlist.rulesHint")}>
        <KeyValueList
          items={[
            {
              label: t("allowlist.hosts"),
              value: <Entries values={data.rules.hosts} empty={tc("state.none")} />,
            },
            {
              label: t("allowlist.suffixes"),
              value: <Entries values={data.rules.suffixes} empty={tc("state.none")} />,
            },
            {
              label: t("allowlist.anyHost"),
              value: data.rules.allow_any ? t("allowlist.anyOn") : t("allowlist.anyOff"),
            },
          ]}
        />
      </Panel>
      <Panel title={t("allowlist.content")} description={t("allowlist.editHint")}>
        {data.truncated ? (
          <p className="mb-2 text-xs text-warn">{t("allowlist.truncated")}</p>
        ) : null}
        <pre className="max-h-[50vh] overflow-auto rounded-md bg-code px-3 py-2 font-mono text-xs whitespace-pre-wrap text-code-ink">
          {data.exists ? data.content || t("allowlist.emptyFile") : t("allowlist.noFile")}
        </pre>
      </Panel>
      <Panel title={t("allowlist.syntax")}>
        <ul className="flex flex-col gap-1 text-sm text-ink-muted">
          <li>
            <code className="font-mono text-ink">pypi.org</code> {t("allowlist.syntaxExact")}
          </li>
          <li>
            <code className="font-mono text-ink">.pypi.org</code> {t("allowlist.syntaxSuffix")}
          </li>
          <li>
            <code className="font-mono text-ink">*</code> {t("allowlist.syntaxAny")}
          </li>
          <li>
            <code className="font-mono text-ink">203.0.113.7</code> {t("allowlist.syntaxIp")}
          </li>
          <li>{t("allowlist.syntaxShell")}</li>
        </ul>
      </Panel>
    </div>
  );
}
