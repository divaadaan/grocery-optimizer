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
- Recipe generation is a background job (local LLMs take minutes):
  `POST /recipes/generate` returns a `job_id` and `useGenerateRecipes` polls
  `GET /recipes/generate/status/{job_id}` every 2s until the job is
  `completed`/`failed`. The hook keeps the mutation-shaped surface
  (`mutate`/`isPending`/`isError`/`error`/`data`) — `isPending` now spans the
  whole run — and adds `job` for live progress. `GenerationProgress` renders
  the server-supplied stage checklist, so stages cannot drift from the graph.
  Jobs live in the API process' memory: a backend restart loses an in-flight
  job, which surfaces as a 404 on the next poll.
