import type { ComponentType } from "react";
import { useTranslation } from "react-i18next";
import { createBrowserRouter, Navigate, Outlet, type RouteObject } from "react-router";
import { PageSpinner } from "@/components/ui/spinner";
import { Toaster } from "@/components/ui/toast";
import { AgentCrumb, CommonCrumb, SessionCrumb, SettingsCrumb } from "./crumbs";
import { AgentLogsCrumb, SettingsExtraCrumb, WorkflowCrumb } from "./crumbs";
import { WorkflowLogsCrumb, WorkflowRunCrumb } from "./crumbs";
import { RequireAuth } from "./guards";
import { AppShell } from "./layout/AppShell";
import type { RouteHandle } from "./layout/contextBar";
import { NotFoundPage, RouteError } from "./RouteError";

// 화면마다 따로 묶어 첫 화면에 필요한 코드만 내려받게 한다.
function page(load: () => Promise<{ default: ComponentType }>) {
  return async () => ({ Component: (await load()).default });
}

function handle(crumb: RouteHandle["crumb"]): RouteHandle {
  return { crumb };
}

function RootLayout() {
  return (
    <>
      <Outlet />
      <Toaster />
    </>
  );
}

// 첫 화면의 코드 조각(lazy)을 받는 동안 보이는 화면
function InitialLoading() {
  const { t } = useTranslation();
  return <PageSpinner label={t("state.loading")} />;
}

// 개발 서버에서만 쓰는 화면. import.meta.env.DEV가 빌드 때 false로 바뀌어 묶음에서 빠진다.
const devRoutes: RouteObject[] = import.meta.env.DEV
  ? [{ path: "/dev/chat-fixture", lazy: page(() => import("@/dev/ChatFixturePage")) }]
  : [];

export const routes: RouteObject[] = [
  {
    element: <RootLayout />,
    errorElement: <RouteError />,
    HydrateFallback: InitialLoading,
    children: [
      { path: "/setup", lazy: page(() => import("./pages/SetupPage")) },
      { path: "/login", lazy: page(() => import("./pages/LoginPage")) },
      ...devRoutes,
      {
        element: <RequireAuth />,
        children: [
          {
            element: <AppShell />,
            children: [
              {
                // 화면 오류가 나도 내비게이션 레일과 상단 바는 남긴다.
                errorElement: <RouteError />,
                children: [
                  { index: true, element: <Navigate to="/agents" replace /> },
                  {
                    path: "agents",
                    handle: handle(() => <CommonCrumb k="nav.agents" />),
                    children: [
                      { index: true, lazy: page(() => import("./pages/AgentsPage")) },
                      {
                        path: ":id",
                        lazy: page(() => import("./pages/AgentBuilderPage")),
                        handle: handle((params) => <AgentCrumb id={params.id ?? ""} />),
                      },
                      {
                        path: ":id/logs",
                        lazy: page(() => import("./pages/LogsPage")),
                        handle: handle((params) => <AgentLogsCrumb id={params.id ?? ""} />),
                      },
                    ],
                  },
                  {
                    path: "workflows",
                    handle: handle(() => <CommonCrumb k="nav.workflows" />),
                    children: [
                      {
                        index: true,
                        lazy: page(() => import("./pages/workflows/WorkflowsPage")),
                      },
                      {
                        path: ":id",
                        lazy: page(() => import("./pages/workflows/WorkflowEditorPage")),
                        handle: {
                          crumb: (params) => <WorkflowCrumb id={params.id ?? ""} />,
                          rail: "collapsed",
                        } satisfies RouteHandle,
                      },
                      {
                        path: ":id/logs",
                        handle: handle((params) => <WorkflowLogsCrumb id={params.id ?? ""} />),
                        children: [
                          {
                            index: true,
                            lazy: page(() => import("./pages/workflows/WorkflowLogsPage")),
                          },
                          {
                            path: ":rid",
                            lazy: page(() => import("./pages/workflows/WorkflowRunPage")),
                            handle: handle((params) => <WorkflowRunCrumb rid={params.rid ?? ""} />),
                          },
                        ],
                      },
                    ],
                  },
                  {
                    path: "skills",
                    lazy: page(() => import("./pages/SkillsPage")),
                    handle: handle(() => <CommonCrumb k="nav.skills" />),
                  },
                  {
                    path: "logs",
                    handle: handle(() => <CommonCrumb k="nav.logs" />),
                    children: [
                      { index: true, lazy: page(() => import("./pages/LogsPage")) },
                      {
                        path: "sessions/:sid",
                        lazy: page(() => import("./pages/SessionLogPage")),
                        handle: handle((params) => <SessionCrumb sid={params.sid ?? ""} />),
                      },
                    ],
                  },
                  {
                    path: "settings",
                    lazy: page(() => import("./pages/settings/SettingsLayout")),
                    handle: handle(() => <CommonCrumb k="nav.settings" />),
                    children: [
                      { index: true, element: <Navigate to="llm" replace /> },
                      {
                        path: "llm",
                        lazy: page(() => import("./pages/settings/LlmSettingsPage")),
                        handle: handle(() => <SettingsCrumb k="nav.llm" />),
                      },
                      {
                        path: "workspace",
                        lazy: page(() => import("./pages/settings/WorkspaceSettingsPage")),
                        handle: handle(() => <SettingsCrumb k="nav.workspace" />),
                      },
                      {
                        path: "workspaces",
                        lazy: page(() => import("./pages/settings/WorkspacesPage")),
                        handle: handle(() => <SettingsCrumb k="nav.workspaces" />),
                      },
                      {
                        path: "usage",
                        lazy: page(() => import("./pages/settings/UsagePage")),
                        handle: handle(() => <SettingsExtraCrumb k="nav.usage" />),
                      },
                      {
                        path: "http-allowlist",
                        lazy: page(() => import("./pages/settings/HttpAllowlistPage")),
                        handle: handle(() => <SettingsExtraCrumb k="nav.httpAllowlist" />),
                      },
                    ],
                  },
                  {
                    path: "account",
                    lazy: page(() => import("./pages/AccountPage")),
                    handle: handle(() => <CommonCrumb k="nav.account" />),
                  },
                  { path: "*", element: <NotFoundPage /> },
                ],
              },
            ],
          },
          {
            // 워크플로우·챗플로우 웹앱(2단계 7.2, 7.4). 워크플로우의 고른 런은 주소의 ?run=이고, 챗플로우는
            // 대화를 바꿔도 화면을 그대로 두도록 대화 경로를 자식으로 둔다.
            path: "flows/:slug",
            lazy: page(() => import("./pages/FlowAppPage")),
            errorElement: <RouteError />,
            children: [
              { index: true, element: null },
              { path: "c/:cid", element: null },
            ],
          },
          {
            // 웹앱(설계 7.2)은 콘솔의 내비게이션 레일 없이 따로 그린다. 대화를 바꿔도 화면은 그대로
            // 두고 주소의 세션만 바꾸도록 세션 경로를 자식으로 둔다.
            path: "apps/:slug",
            lazy: page(() => import("./pages/WebAppPage")),
            errorElement: <RouteError />,
            children: [
              { index: true, element: null },
              { path: "s/:sid", element: null },
            ],
          },
        ],
      },
    ],
  },
];

export function createRouter() {
  return createBrowserRouter(routes);
}
