import { Bot, ChevronRight, LogOut, Puzzle, ScrollText, Settings, UserRound } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useMatches, useNavigate } from "react-router";
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
} from "@/components/ui/menu";
import { toast } from "@/components/ui/toast";
import { useLogout, useMe } from "@/features/auth/api";
import { cn } from "@/lib/cn";
import { errorMessage } from "@/lib/errors";
import { ContextBarSlotContext, type RouteHandle } from "./contextBar";

const NAV_ITEMS = [
  { to: "/agents", icon: Bot, key: "nav.agents" },
  { to: "/skills", icon: Puzzle, key: "nav.skills" },
  { to: "/logs", icon: ScrollText, key: "nav.logs" },
  { to: "/settings", icon: Settings, key: "nav.settings" },
] as const;

function railLinkClass({ isActive }: { isActive: boolean }) {
  return cn(
    "flex w-14 flex-col items-center gap-1 rounded-md py-2 text-[11px] transition-colors",
    isActive
      ? "bg-rail-active text-rail-ink-active"
      : "text-rail-ink hover:bg-rail-active/60 hover:text-rail-ink-active",
  );
}

function NavRail() {
  const { t } = useTranslation();
  return (
    <nav
      aria-label={t("nav.main")}
      className="flex h-full flex-col items-center gap-4 bg-rail py-3"
    >
      <Link
        to="/agents"
        className="grid size-9 place-items-center rounded-md bg-rail-active text-base font-bold text-rail-mark"
        aria-label={t("appName")}
      >
        W
      </Link>
      <ul className="flex flex-col gap-1">
        {NAV_ITEMS.map(({ to, icon: Icon, key }) => (
          <li key={to}>
            <NavLink to={to} className={railLinkClass}>
              <Icon className="size-5" aria-hidden />
              <span>{t(key)}</span>
            </NavLink>
          </li>
        ))}
      </ul>
      <div className="mt-auto">
        <NavLink to="/account" className={railLinkClass}>
          <UserRound className="size-5" aria-hidden />
          <span>{t("nav.account")}</span>
        </NavLink>
      </div>
    </nav>
  );
}

function Breadcrumbs() {
  const { t } = useTranslation();
  const matches = useMatches();
  const crumbs = matches.flatMap((match) => {
    const handle = match.handle as RouteHandle | undefined;
    if (!handle?.crumb) return [];
    return [{ id: match.id, to: match.pathname, node: handle.crumb(match.params) }];
  });
  return (
    <nav aria-label={t("nav.breadcrumb")} className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-sm">
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          return (
            <li
              key={crumb.id}
              className={cn(
                "flex min-w-0 items-center gap-1.5",
                last ? "font-medium text-ink" : "text-ink-muted",
              )}
            >
              {index > 0 ? (
                <ChevronRight className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
              ) : null}
              {last ? (
                <span className="truncate" aria-current="page">
                  {crumb.node}
                </span>
              ) : (
                <Link to={crumb.to} className="truncate hover:text-ink">
                  {crumb.node}
                </Link>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function AccountMenu() {
  const { t } = useTranslation();
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const account = me.data?.account;
  return (
    <Menu>
      <MenuTrigger
        className="grid size-8 place-items-center rounded-full bg-sunken text-sm font-semibold text-ink-muted hover:bg-hover"
        aria-label={t("account.menu")}
      >
        {(account?.name ?? "?").slice(0, 1).toUpperCase()}
      </MenuTrigger>
      <MenuContent align="end" className="w-60">
        <MenuLabel>
          <span className="block truncate font-medium text-ink">{account?.name}</span>
          <span className="block truncate">{account?.email}</span>
          {me.data ? (
            <span className="mt-1 block truncate">
              {t("account.workspace", { name: me.data.workspace.name })}
            </span>
          ) : null}
        </MenuLabel>
        <MenuSeparator />
        <MenuItem onSelect={() => navigate("/account")}>
          <UserRound className="size-4" aria-hidden />
          {t("nav.account")}
        </MenuItem>
        <MenuItem
          onSelect={() => {
            logout.mutate(undefined, {
              onSettled: (_data, error) => {
                if (error) toast.error(errorMessage(error, t));
                navigate("/login", { replace: true });
              },
            });
          }}
        >
          <LogOut className="size-4" aria-hidden />
          {t("actions.logout")}
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

export function AppShell() {
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  return (
    <div className="grid h-full grid-cols-[var(--wf-rail-width)_minmax(0,1fr)]">
      <NavRail />
      <div className="flex min-h-0 min-w-0 flex-col">
        <header className="flex h-[var(--wf-contextbar-height)] shrink-0 items-center gap-3 border-b border-line bg-surface px-4">
          <Breadcrumbs />
          <div ref={setSlot} className="ml-auto flex items-center gap-2" />
          <AccountMenu />
        </header>
        <ContextBarSlotContext value={slot}>
          <main className="min-h-0 flex-1 overflow-auto">
            <Outlet />
          </main>
        </ContextBarSlotContext>
      </div>
    </div>
  );
}
