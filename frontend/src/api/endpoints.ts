/**
 * Typed endpoint functions, one per backend route.
 * Pure transport — no caching or UI concerns (those live in src/hooks).
 */

import { API_PREFIX, get, post, put, del } from "./client";
import type {
  ApiInfo,
  DealInfo,
  HealthCheckResponse,
  PostalCodeDiscoveryResponse,
  RecipeGenerationRequest,
  RecipeGenerationResponse,
  RecipeInfo,
  ShoppingListResponse,
  UserCreate,
  UserResponse,
  UserUpdate,
} from "./types";

// Users -----------------------------------------------------------------

export const registerUser = (data: UserCreate) =>
  post<UserResponse>(`${API_PREFIX}/users/register`, data);

export const getUser = (userId: number) =>
  get<UserResponse>(`${API_PREFIX}/users/${userId}`);

export const updateUser = (userId: number, data: UserUpdate) =>
  put<UserResponse>(`${API_PREFIX}/users/${userId}`, data);

export const deactivateUser = (userId: number) =>
  del<void>(`${API_PREFIX}/users/${userId}`);

// Stores & deals --------------------------------------------------------

export const discoverPostalCode = (postalCode: string) =>
  post<PostalCodeDiscoveryResponse>(`${API_PREFIX}/postal-code/discover`, {
    postal_code: postalCode,
  });

export const getDeals = (postalCode: string, category?: string, limit = 100) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (category) params.set("category", category);
  return get<DealInfo[]>(
    `${API_PREFIX}/postal-code/deals/${encodeURIComponent(postalCode)}?${params}`,
  );
};

export const getTopDeals = (postalCode: string, limit = 20) =>
  get<DealInfo[]>(
    `${API_PREFIX}/postal-code/top-deals/${encodeURIComponent(postalCode)}?limit=${limit}`,
  );

export const searchDeals = (postalCode: string, q: string) =>
  get<DealInfo[]>(
    `${API_PREFIX}/postal-code/search/${encodeURIComponent(postalCode)}?q=${encodeURIComponent(q)}`,
  );

// Recipes ---------------------------------------------------------------

export const generateRecipes = (req: RecipeGenerationRequest) =>
  post<RecipeGenerationResponse>(`${API_PREFIX}/recipes/generate`, req);

export const getRecipe = (recipeId: number) =>
  get<RecipeInfo>(`${API_PREFIX}/recipes/${recipeId}`);

export const getUserRecipes = (userId: number, limit = 10) =>
  get<RecipeInfo[]>(`${API_PREFIX}/recipes/user/${userId}?limit=${limit}`);

// Shopping lists --------------------------------------------------------

export const getShoppingList = (userId: number) =>
  get<ShoppingListResponse>(`${API_PREFIX}/shopping-list/${userId}`);

// System ----------------------------------------------------------------

export const getHealth = () => get<HealthCheckResponse>("/health");

export const getApiInfo = () => get<ApiInfo>(`${API_PREFIX}/info`);
