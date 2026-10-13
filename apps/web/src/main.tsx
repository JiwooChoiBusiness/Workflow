import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router";
import { setUnauthorizedHandler } from "@/api/http";
import { createQueryClient } from "@/app/queryClient";
import { createRouter } from "@/app/router";
import { authKeys } from "@/features/auth/api";
import "@/i18n";
import "@/styles/index.css";

const queryClient = createQueryClient();
const router = createRouter();

// 로그인 세션이 끝나면(다른 곳에서 비밀번호 변경, 만료) 어느 화면에서든 로그인 화면으로 보낸다.
setUnauthorizedHandler(() => {
  queryClient.removeQueries({ queryKey: authKeys.me });
  const { pathname, search } = router.state.location;
  if (pathname === "/login" || pathname === "/setup") return;
  void router.navigate(`/login?next=${encodeURIComponent(pathname + search)}`, { replace: true });
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
