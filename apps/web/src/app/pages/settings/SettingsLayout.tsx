import { useTranslation } from "react-i18next";
import { NavLink, Outlet } from "react-router";
import { cn } from "@/lib/cn";

const ITEMS = [
  { to: "/settings/llm", key: "nav.llm" },
  { to: "/settings/workspace", key: "nav.workspace" },
  { to: "/settings/workspaces", key: "nav.workspaces" },
  { to: "/settings/usage", key: "nav.usage" },
  { to: "/settings/http-allowlist", key: "nav.httpAllowlist" },
] as const;

export default function SettingsLayout() {
  const { t } = useTranslation("settings");
  return (
    <div className="mx-auto flex max-w-6xl gap-8 px-6 py-6">
      <nav aria-label={t("navLabel")} className="w-44 shrink-0">
        <h1 className="mb-3 px-2 text-xs font-semibold tracking-wide text-ink-subtle">
          {t("title")}
        </h1>
        <ul className="flex flex-col gap-0.5">
          {ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "block rounded-md px-2 py-1.5 text-sm",
                    isActive
                      ? "bg-accent-soft font-medium text-ink"
                      : "text-ink-muted hover:bg-hover",
                  )
                }
              >
                {t(item.key)}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
