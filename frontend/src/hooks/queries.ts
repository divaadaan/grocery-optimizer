/**
 * React-query hooks: the only place query keys and cache policy live.
 * Components consume these; they never call the API layer directly.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "../api/endpoints";
import { ApiError } from "../api/client";
import type {
  RecipeGenerationRequest,
  UserCreate,
  UserUpdate,
} from "../api/types";

export const queryKeys = {
  health: ["health"] as const,
  apiInfo: ["api-info"] as const,
  user: (id: number) => ["user", id] as const,
  deals: (postal: string, mode: string, param?: string) =>
    ["deals", postal, mode, param ?? ""] as const,
  userRecipes: (userId: number) => ["recipes", "user", userId] as const,
  shoppingList: (userId: number) => ["shopping-list", userId] as const,
};

const noRetryOn404or501 = (failureCount: number, error: Error) => {
  if (error instanceof ApiError && (error.status === 404 || error.status === 501)) {
    return false;
  }
  return failureCount < 2;
};

// System ----------------------------------------------------------------

export const useHealth = () =>
  useQuery({
    queryKey: queryKeys.health,
    queryFn: api.getHealth,
    refetchInterval: 60_000,
    retry: false,
  });

export const useApiInfo = () =>
  useQuery({ queryKey: queryKeys.apiInfo, queryFn: api.getApiInfo, staleTime: Infinity });

// Users -----------------------------------------------------------------

export const useRegisterUser = () =>
  useMutation({ mutationFn: (data: UserCreate) => api.registerUser(data) });

export const useLoadUser = () =>
  useMutation({ mutationFn: (userId: number) => api.getUser(userId) });

export const useUpdateUser = (userId: number) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: UserUpdate) => api.updateUser(userId, data),
    onSuccess: (user) => queryClient.setQueryData(queryKeys.user(userId), user),
  });
};

// Deals -----------------------------------------------------------------

export type DealsMode =
  | { kind: "browse"; category?: string }
  | { kind: "top" }
  | { kind: "search"; q: string };

export const useDeals = (postalCode: string, mode: DealsMode) =>
  useQuery({
    queryKey: queryKeys.deals(
      postalCode,
      mode.kind,
      mode.kind === "search" ? mode.q : mode.kind === "browse" ? mode.category : undefined,
    ),
    queryFn: () => {
      switch (mode.kind) {
        case "browse":
          return api.getDeals(postalCode, mode.category);
        case "top":
          return api.getTopDeals(postalCode);
        case "search":
          return api.searchDeals(postalCode, mode.q);
      }
    },
    enabled: postalCode.length >= 6 && (mode.kind !== "search" || mode.q.length >= 2),
    retry: noRetryOn404or501,
  });

export const useDiscoverPostalCode = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (postalCode: string) => api.discoverPostalCode(postalCode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["deals"] }),
  });
};

// Recipes ---------------------------------------------------------------

export const useGenerateRecipes = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req: RecipeGenerationRequest) => api.generateRecipes(req),
    onSuccess: (_data, req) =>
      queryClient.invalidateQueries({ queryKey: queryKeys.userRecipes(req.user_id) }),
  });
};

export const useUserRecipes = (userId: number | undefined) =>
  useQuery({
    queryKey: queryKeys.userRecipes(userId ?? -1),
    queryFn: () => api.getUserRecipes(userId!),
    enabled: userId !== undefined,
    retry: noRetryOn404or501,
  });

// Shopping list ----------------------------------------------------------

export const useShoppingList = (userId: number | undefined) =>
  useQuery({
    queryKey: queryKeys.shoppingList(userId ?? -1),
    queryFn: () => api.getShoppingList(userId!),
    enabled: userId !== undefined,
    retry: noRetryOn404or501,
  });
