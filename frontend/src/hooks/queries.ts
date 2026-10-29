/**
 * React-query hooks: the only place query keys and cache policy live.
 * Components consume these; they never call the API layer directly.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as api from "../api/endpoints";
import { ApiError } from "../api/client";
import type {
  JobState,
  RecipeGenerationJobStatus,
  RecipeGenerationRequest,
  RecipeGenerationResponse,
  UserCreate,
  UserUpdate,
} from "../api/types";

export const queryKeys = {
  health: ["health"] as const,
  apiInfo: ["api-info"] as const,
  user: (id: number) => ["user", id] as const,
  supportedRestrictions: ["supported-restrictions"] as const,
  deals: (postal: string, mode: string, param?: string) =>
    ["deals", postal, mode, param ?? ""] as const,
  userRecipes: (userId: number) => ["recipes", "user", userId] as const,
  generationJob: (jobId: string) => ["recipes", "generation-job", jobId] as const,
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

/** The restriction labels the backend enforces (static per deployment). */
export const useSupportedRestrictions = () =>
  useQuery({
    queryKey: queryKeys.supportedRestrictions,
    queryFn: api.getSupportedRestrictions,
    staleTime: Infinity,
    select: (data) => data.supported,
  });

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

const TERMINAL_JOB_STATES: readonly JobState[] = ["completed", "failed"];
const isTerminal = (state?: JobState) => !!state && TERMINAL_JOB_STATES.includes(state);

/** Generation takes minutes; poll often enough that stages feel live. */
const JOB_POLL_INTERVAL_MS = 2_000;

/** Polls one generation job, stopping once it reaches a terminal state. */
export const useGenerationJob = (jobId: string | undefined) =>
  useQuery({
    queryKey: queryKeys.generationJob(jobId ?? ""),
    queryFn: () => api.getGenerationStatus(jobId!),
    enabled: jobId !== undefined,
    refetchInterval: (query) =>
      isTerminal(query.state.data?.state) ? false : JOB_POLL_INTERVAL_MS,
    // A job's stage changes constantly; never serve it from cache.
    staleTime: 0,
    gcTime: 0,
    retry: noRetryOn404or501,
  });

export interface RecipeGeneration {
  /** Start a generation. Same call signature as the old mutation. */
  mutate: (req: RecipeGenerationRequest) => void;
  reset: () => void;
  /** True from the POST until the job reaches a terminal state. */
  isPending: boolean;
  isError: boolean;
  error: Error | null;
  /** The finished meal plan — the same payload the sync endpoint returned. */
  data: RecipeGenerationResponse | undefined;
  /** Live progress, for GenerationProgress. */
  job: RecipeGenerationJobStatus | undefined;
}

/**
 * Recipe generation as a background job (ROADMAP item 2): POST returns a
 * `job_id`, progress and the result arrive by polling. The surface mirrors
 * the react-query mutation this replaced (`mutate` / `isPending` /
 * `isError` / `error` / `data`) so callers only need the extra `job` field,
 * with `isPending` now spanning the whole run rather than one HTTP request.
 */
export const useGenerateRecipes = (): RecipeGeneration => {
  const queryClient = useQueryClient();
  const [jobId, setJobId] = useState<string>();

  const start = useMutation({
    mutationFn: (req: RecipeGenerationRequest) => api.startRecipeGeneration(req),
    onMutate: () => setJobId(undefined),
    onSuccess: (job) => setJobId(job.job_id),
  });

  const job = useGenerationJob(jobId);
  const state = job.data?.state;
  const userId = start.variables?.user_id;

  // Recipes are persisted by the workflow's final node, so the history view
  // can only be refreshed once the job completes — not when the POST returns.
  useEffect(() => {
    if (state === "completed" && userId !== undefined) {
      queryClient.invalidateQueries({ queryKey: queryKeys.userRecipes(userId) });
    }
  }, [state, userId, queryClient]);

  const jobFailure =
    state === "failed"
      ? new Error(job.data?.error ?? "Recipe generation failed.")
      : null;

  return {
    mutate: start.mutate,
    reset: () => {
      setJobId(undefined);
      start.reset();
    },
    // Awaiting the POST, awaiting the first poll, or mid-run.
    isPending:
      start.isPending || (jobId !== undefined && !isTerminal(state) && !job.isError),
    isError: start.isError || job.isError || jobFailure !== null,
    error: start.error ?? job.error ?? jobFailure,
    data: state === "completed" ? job.data?.result ?? undefined : undefined,
    job: job.data,
  };
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
