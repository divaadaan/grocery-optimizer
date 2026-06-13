import { useMemo, useState } from "react";
import { Card, EmptyState, ErrorBanner, Spinner } from "../../components/ui";
import { DealCard } from "./DealCard";
import { useCurrentUser } from "../user/UserContext";
import { useDeals, useDiscoverPostalCode, type DealsMode } from "../../hooks/queries";
import { ApiError } from "../../api/client";

export function DealsPage() {
  const { user } = useCurrentUser();
  const [postalCode, setPostalCode] = useState(user?.postal_code ?? "");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [topOnly, setTopOnly] = useState(false);

  const mode: DealsMode = search.trim().length >= 2
    ? { kind: "search", q: search.trim() }
    : topOnly
      ? { kind: "top" }
      : { kind: "browse", category: category || undefined };

  const normalizedPostal = postalCode.replace(/\s+/g, "").toUpperCase();
  const deals = useDeals(normalizedPostal, mode);
  const discover = useDiscoverPostalCode();

  // Categories come from the unfiltered browse result currently in cache —
  // good enough for a filter bar without a dedicated endpoint.
  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const deal of deals.data ?? []) if (deal.category) set.add(deal.category);
    if (category) set.add(category);
    return [...set].sort();
  }, [deals.data, category]);

  const noDealsYet =
    deals.isError && deals.error instanceof ApiError && deals.error.status === 404;

  return (
    <div className="page">
      <h1>Local deals</h1>

      <Card>
        <div className="toolbar">
          <input
            className="toolbar-postal"
            value={postalCode}
            onChange={(e) => setPostalCode(e.target.value.toUpperCase())}
            placeholder="Postal code"
            maxLength={10}
          />
          <button
            type="button"
            className="button"
            disabled={normalizedPostal.length < 6 || discover.isPending}
            onClick={() => discover.mutate(normalizedPostal)}
          >
            {discover.isPending ? "Discovering…" : "Discover stores"}
          </button>
          <input
            className="toolbar-search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search products or brands…"
          />
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <label className="toggle">
            <input
              type="checkbox"
              checked={topOnly}
              onChange={(e) => setTopOnly(e.target.checked)}
            />
            Top deals
          </label>
        </div>
        {discover.isSuccess && (
          <div className="banner banner-success">{discover.data.message}</div>
        )}
        {discover.isError && <ErrorBanner error={discover.error} />}
      </Card>

      {normalizedPostal.length < 6 ? (
        <EmptyState
          title="Enter a postal code"
          message="Deals are local — set a postal code above (or in your profile) to browse."
        />
      ) : deals.isPending ? (
        <Spinner label="Loading deals…" />
      ) : noDealsYet ? (
        <EmptyState
          title="No deals found"
          message={`Nothing on file for ${normalizedPostal} yet. Try "Discover stores" to fetch stores and deals for this area.`}
        />
      ) : deals.isError ? (
        <ErrorBanner error={deals.error} />
      ) : deals.data.length === 0 ? (
        <EmptyState title="No matches" message="Try a different search term or filter." />
      ) : (
        <div className="grid-cards">
          {deals.data.map((deal) => (
            <DealCard key={`${deal.deal_id}-${deal.store_name}`} deal={deal} />
          ))}
        </div>
      )}
    </div>
  );
}
