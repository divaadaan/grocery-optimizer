# Grocery Optimizer — Frontend

React SPA for the FastAPI backend. Vite + React 19 + TypeScript,
[@tanstack/react-query](https://tanstack.com/query) for server state,
react-router for navigation. No UI framework — a small token-based CSS layer
(`src/styles/tokens.css`).

## Running

Node lives in WSL (installed via nvm — `npm` does not work from a Windows
shell against this repo's UNC path). From a WSL terminal:

```bash
cd ~/projects/grocery-optimizer/frontend
npm install
npm run dev        # http://localhost:3000, proxies /api and /health to :8000
npm run build      # typecheck + production bundle in dist/
```

The backend must be running on `localhost:8000` (`docker compose up` or
`uvicorn app.main:app`). For a deployed backend set `VITE_API_BASE_URL`.

## Architecture

Strict layering — each layer only imports from the one below:

```
src/
  api/        transport: types.ts mirrors app/models/schemas.py exactly
              (snake_case wire format); client.ts is the single fetch
              wrapper (ApiError, 501-stub detection); endpoints.ts has one
              typed function per backend route
  hooks/      queries.ts: all react-query hooks, query keys, cache policy.
              Components never call api/ directly.
  components/ shared primitives (Card, Badge, Spinner, ErrorBanner,
              EmptyState, Field, QueryError, ChipsInput) and the Layout shell
  features/   one folder per domain: user (profile + localStorage session),
              deals, planner, shopping, home
  utils/      pure formatting helpers (money, dates, durations)
```

Conventions:

- Backend `Decimal` fields arrive as JSON strings; the `Money` type and
  `formatMoney` handle both.
- Stub endpoints (501) render as "coming soon" panels via `QueryError`,
  so pages activate as backend roadmap items land.
- Recipe generation is synchronous and slow (local LLMs); `GenerationProgress`
  shows the real pipeline stages + elapsed time. When the async job API lands
  (ROADMAP item 2), only `useGenerateRecipes` and `GenerationProgress` change.
