import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { isApiError } from "@/api/http";
import type {
  LoginRequest,
  MeOut,
  PasswordChangeRequest,
  SetupRequest,
  SetupStatus,
} from "@/api/types";

export const authKeys = {
  me: ["auth", "me"] as const,
  setup: ["setup"] as const,
};

export function useMe() {
  return useQuery({
    queryKey: authKeys.me,
    queryFn: () => unwrap(api.GET("/api/console/auth/me")),
    retry: false,
    staleTime: 5 * 60_000,
  });
}

// 계정이 생긴 뒤에는 GET /setup도 404다(설계 6.2). 404를 "설정 끝남"으로 읽는다.
export function useSetupStatus() {
  return useQuery({
    queryKey: authKeys.setup,
    queryFn: async (): Promise<SetupStatus> => {
      try {
        return await unwrap(api.GET("/api/console/setup"));
      } catch (error) {
        if (isApiError(error, 404)) return { needs_setup: false, password_min_length: 0 };
        throw error;
      }
    },
    retry: false,
    staleTime: Infinity,
  });
}

export function useSetup() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SetupRequest) => unwrap(api.POST("/api/console/setup", { body })),
    onSuccess: () => {
      client.setQueryData<SetupStatus>(authKeys.setup, {
        needs_setup: false,
        password_min_length: 0,
      });
    },
  });
}

export function useLogin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: LoginRequest) => unwrap(api.POST("/api/console/auth/login", { body })),
    onSuccess: (me: MeOut) => {
      client.setQueryData(authKeys.me, me);
    },
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/console/auth/logout")),
    onSettled: () => {
      // 로그아웃이 실패해도(이미 만료 등) 화면에 남은 데이터는 모두 버린다.
      client.clear();
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (body: PasswordChangeRequest) =>
      unwrap(api.POST("/api/console/auth/password", { body })),
  });
}
