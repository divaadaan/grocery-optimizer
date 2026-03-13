# API Call Dependencies

This document describes ordering requirements and data dependencies between API endpoints.

## Dependency Graph

```
POST /api/v1/users/register
  └─► POST /api/v1/postal-code/discover  (user's postal_code seeded into system)
        └─► POST /api/v1/recipes/generate  (requires user_id + deals to exist)
              └─► GET /api/v1/shopping-list/{user_id}  [not yet implemented]
                    └─► POST /api/v1/shopping-list/{user_id}/mark-complete  [not yet implemented]
```

---

## Required Call Sequences

### 1. New User → Meal Plan (Happy Path)

You must complete these steps in order before a meal plan can be generated.

| Step | Call | Why |
|------|------|-----|
| 1 | `POST /api/v1/users/register` | Creates the user, returns `user_id` |
| 2 | `POST /api/v1/postal-code/discover` with the user's postal code | Fetches stores and loads deals into the DB/cache |
| 3 | `POST /api/v1/recipes/generate` with `user_id` from step 1 | Needs the user to exist and deals to be available for the user's postal code |

**If step 2 is skipped**, recipe generation will either fail or produce results with no real pricing data because there are no deals in the system for that postal code.

---

### 2. Browse Deals Before Generating

Deals endpoints can be called independently once discovery has run.

| Step | Call | Why |
|------|------|-----|
| 1 | `POST /api/v1/postal-code/discover` | Populates stores and deals |
| 2 | `GET /api/v1/postal-code/deals/{postal_code}` | Browsing all deals |
| 2 | `GET /api/v1/postal-code/top-deals/{postal_code}` | View highest discounts |
| 2 | `GET /api/v1/postal-code/search/{postal_code}?q=...` | Search specific products |

Steps 2–4 in this group are all independent of each other (can run in any order or in parallel after discovery).

---

### 3. Shopping List Flow (future — not yet implemented)

Once recipes exist, the shopping list can be retrieved and then marked complete.

| Step | Call | Why |
|------|------|-----|
| 1 | `POST /api/v1/recipes/generate` | Creates recipes saved to the DB |
| 2 | `GET /api/v1/shopping-list/{user_id}` | Aggregates ingredients from saved recipes |
| 3 | `POST /api/v1/shopping-list/{user_id}/mark-complete` | Marks the list done |

Step 3 requires a shopping list to exist from step 2's backing data.

---

## ID Propagation

These IDs are returned by one call and consumed by another:

| Produced by | Field | Consumed by |
|-------------|-------|-------------|
| `POST /api/v1/users/register` | `user_id` | `POST /api/v1/recipes/generate` → body `user_id` |
| `POST /api/v1/users/register` | `postal_code` (echoed) | `POST /api/v1/postal-code/discover` → body `postal_code` |
| `POST /api/v1/recipes/generate` | `recipes[].recipe_id` | `GET /api/v1/recipes/{recipe_id}` *(not implemented)* |
| `POST /api/v1/users/register` | `user_id` | `GET /api/v1/recipes/user/{user_id}` *(not implemented)* |
| `POST /api/v1/users/register` | `user_id` | `GET /api/v1/shopping-list/{user_id}` *(not implemented)* |

---

## Caching Behavior

Calls that hit Redis cache — re-running them within the TTL window returns the cached result without hitting the DB or external services:

| Endpoint | Cache Key Pattern | TTL |
|----------|-------------------|-----|
| `POST /api/v1/postal-code/discover` | `deals:{postal_code}:{category}` | 6 hours |
| `GET /api/v1/postal-code/deals/{postal_code}` | `deals:{postal_code}:{category}` | 6 hours |
| `GET /api/v1/postal-code/top-deals/{postal_code}` | `deals:{postal_code}:{category}` | 6 hours |
| `GET /api/v1/postal-code/search/{postal_code}` | `deals:{postal_code}:{category}` | 6 hours |
| Recipe results | *(recipe cache key)* | 24 hours |
| Store records | *(store cache key)* | 7 days |

If you need fresh deal data during testing, either wait for TTL expiry or flush Redis.

---

## Independent Endpoints (No Prerequisites)

These can be called at any time with no dependencies:

- `GET /health`
- `GET /api/v1/info`
- `POST /api/v1/users/register`
- `GET /api/v1/users/{user_id}` *(user must exist, but no prior API call required beyond registration)*
- `PUT /api/v1/users/{user_id}`
- `DELETE /api/v1/users/{user_id}`
