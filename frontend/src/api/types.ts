/**
 * TypeScript mirrors of the backend Pydantic schemas (app/models/schemas.py).
 * Keep field names snake_case to match the wire format exactly.
 */

/** Pydantic v2 serializes Decimal as a JSON string; tolerate both. */
export type Money = number | string;

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

export interface UserCreate {
  email: string;
  postal_code: string;
  budget: number;
  household_size: number;
  dietary_restrictions: string[];
}

export interface UserResponse {
  user_id: number;
  email: string;
  postal_code: string;
  budget: Money;
  household_size: number;
  dietary_restrictions: string[];
  created_at: string;
  is_active: boolean;
}

export type UserUpdate = Partial<Omit<UserCreate, "email">>;

/** GET /users/dietary-restrictions: the labels the backend enforces. Any
 *  other label is refused with 422 by register / update. */
export interface DietaryRestrictionsResponse {
  supported: string[];
}

// ---------------------------------------------------------------------------
// Stores & deals
// ---------------------------------------------------------------------------

export interface StoreInfo {
  store_id: number;
  name: string;
  chain: string | null;
  postal_code: string;
  address: string | null;
  city: string | null;
  province: string | null;
}

export interface DealInfo {
  deal_id: number;
  product_name: string;
  brand: string | null;
  sale_price: Money;
  regular_price: Money;
  discount_percentage: number;
  unit: string | null;
  category: string | null;
  valid_from: string | null;
  valid_until: string | null;
  store_name: string;
  chain: string | null;
}

export interface PostalCodeDiscoveryResponse {
  postal_code: string;
  stores_found: number;
  deals_count: number;
  stores: StoreInfo[];
  job_id: string | null;
  message: string;
}

// ---------------------------------------------------------------------------
// Recipes
// ---------------------------------------------------------------------------

export interface RecipeIngredient {
  name?: string;
  quantity?: string | number;
  unit?: string;
  price?: Money | null;
  [key: string]: unknown;
}

export interface RecipeInfo {
  recipe_id: number;
  name: string;
  ingredients: RecipeIngredient[];
  instructions: string[];
  total_cost: Money;
  servings: number;
  estimated_prep_time: number | null;
  meal_type: string | null;
  cuisine_type: string | null;
  nutrition_facts: Record<string, unknown> | null;
  health_score: Money | null;
  created_at: string;
  /** The meal plan (generation) this recipe belongs to; null for legacy rows. */
  generation_id?: string | null;
}

export interface RecipeGenerationPreferences {
  cuisine_preferences?: string[];
  avoid_ingredients?: string[];
  meal_types?: string[];
}

export interface RecipeGenerationRequest {
  user_id: number;
  num_meals: number;
  preferences: RecipeGenerationPreferences;
  /** Deals kept from POST /plans/trays; omitted = keep everything offered. */
  kept_deal_ids?: number[];
  /** Pantry staples confirmed; omitted = all offered. */
  pantry_product_ids?: number[];
}

// Trays (POST /plans/trays) ----------------------------------------------

export type TrayName = "anchors" | "sides" | "stock_up";

/** One tray item: the cheapest deal for its item, and how it compares across chains. */
export interface TrayItem {
  deal_id: number;
  product_name: string;
  store_name: string | null;
  chain: string | null;
  sale_price: number;
  unit: string | null;
  item: string;
  role: string;
  /** Price basis of compare_price / median_price. */
  basis: "lb" | "l" | "each" | "pkg";
  compare_price: number;
  median_price: number;
  n_chains: number;
  /** 1 - compare_price / median_price; 0 when only one chain has it. */
  gap: number;
}

export interface PantryItem {
  product_id: number;
  name: string;
}

export interface TrayProposalRequest {
  user_id: number;
  num_meals: number;
}

export interface TrayProposalResponse {
  user_id: number;
  postal_code: string;
  num_meals: number;
  trays: Record<TrayName, TrayItem[]>;
  pantry: PantryItem[];
  counts: Record<string, number>;
  label_version: string;
  warnings: string[];
}

export interface RecipeGenerationResponse {
  recipes: RecipeInfo[];
  total_cost: Money;
  cost_per_meal: Money;
  estimated_savings: Money | null;
  generation_time: number;
  status: string;
  warnings: string[];
  /** Identifies this meal plan; the shopping list is built per generation. */
  generation_id?: string | null;
}

/** Generation is a background job: POST /recipes/generate returns this. */
export type JobState = "pending" | "running" | "completed" | "failed";

export interface RecipeGenerationJobCreated {
  job_id: string;
  user_id: number;
  state: JobState;
  created_at: string;
  status_url: string;
}

export interface RecipeGenerationJobStatus {
  job_id: string;
  user_id: number;
  state: JobState;
  /** Human-readable current work, e.g. "SousChef 2/3 cooking…". */
  stage: string;
  /** 1-based position in `pipeline`; 0 while the job is still queued. */
  stage_index: number;
  stage_total: number;
  /** The nominal stage checklist, server-supplied so it cannot drift. */
  pipeline: string[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_seconds: number;
  /** Set once state === "completed". */
  result: RecipeGenerationResponse | null;
  /** Set once state === "failed". */
  error: string | null;
}

// ---------------------------------------------------------------------------
// Shopping lists
// ---------------------------------------------------------------------------

export interface ShoppingListItem {
  product?: string;
  quantity?: string;
  store?: string;
  price?: Money | null;
  category?: string | null;
  [key: string]: unknown;
}

export interface ShoppingListResponse {
  list_id: number;
  user_id: number;
  recipe_ids: number[];
  items: ShoppingListItem[];
  total_cost: Money;
  estimated_savings: Money | null;
  stores: string[];
  created_at: string;
  is_completed: boolean;
  /** The generation the list was built from; null for legacy lists. */
  generation_id?: string | null;
}

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export interface HealthCheckResponse {
  status: string;
  version: string;
  database: string;
  redis: string | null;
  ollama: string | null;
  timestamp: string;
}

export interface ApiInfo {
  version: string;
  environment: string;
  features: {
    redis_caching: boolean;
    cost_tracking: boolean;
    /** Model spec per role, e.g. "ollama:qwen2.5:7b" (approach C). */
    models: {
      labeller: string;
      chef: string;
      sous_chef: string;
    };
  };
  limits: Record<string, string>;
}
