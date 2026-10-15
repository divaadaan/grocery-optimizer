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
}

export interface RecipeGenerationResponse {
  recipes: RecipeInfo[];
  total_cost: Money;
  cost_per_meal: Money;
  estimated_savings: Money | null;
  generation_time: number;
  status: string;
  warnings: string[];
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
    ollama_models: {
      chef: string;
      sous_chef: string;
      nutritionist: string;
    };
  };
  limits: Record<string, string>;
}
