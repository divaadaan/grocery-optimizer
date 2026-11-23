import type { PantryItem, TrayItem, TrayName } from "../../api/types";
import { formatMoney, humanize } from "../../utils/format";

/**
 * Approach C step 1: the trays the server built from this week's gated deals
 * (POST /plans/trays). Every card starts kept; the user rejects what they do
 * not want. The kept ids are sent with the generation request, and the server
 * re-checks them against its own trays.
 */

export const TRAY_TITLES: Record<TrayName, { title: string; hint: string }> = {
  anchors: {
    title: "Anchors",
    hint: "This week's protein deals, each the cheapest for its item nearby. Dishes are built around these.",
  },
  sides: { title: "Sides", hint: "Vegetables, starches, fruit and dairy on sale." },
  stock_up: {
    title: "Stock-up",
    hint: "Discounted pantry staples sold by at least two chains: worth buying extra.",
  },
};

const BASIS_SUFFIX: Record<TrayItem["basis"], string> = {
  lb: "/lb",
  l: "/L",
  each: " each",
  pkg: "",
};

function comparison(item: TrayItem): string {
  if (item.n_chains < 2) return "Only chain with it this week";
  const pct = Math.round(item.gap * 100);
  const median = `${formatMoney(item.median_price)}${BASIS_SUFFIX[item.basis]}`;
  return pct > 0
    ? `${pct}% under the ${item.n_chains}-chain median (${median})`
    : `Cheapest of ${item.n_chains} chains (median ${median})`;
}

function TrayCard({
  item,
  kept,
  onToggle,
}: {
  item: TrayItem;
  kept: boolean;
  onToggle: () => void;
}) {
  const perUnit =
    item.basis !== "pkg"
      ? ` · ${formatMoney(item.compare_price)}${BASIS_SUFFIX[item.basis]}`
      : "";
  return (
    <button
      type="button"
      className={`tray-card ${kept ? "tray-card-kept" : "tray-card-rejected"}`}
      aria-pressed={kept}
      onClick={onToggle}
      title={kept ? "Click to reject" : "Click to keep"}
    >
      <span className="tray-card-item">{humanize(item.item)}</span>
      <span className="tray-card-name">{item.product_name}</span>
      <span className="tray-card-price">
        {formatMoney(item.sale_price)}
        {item.unit ? `/${item.unit}` : ""}
        {perUnit}
      </span>
      <span className="tray-card-store">{item.chain ?? item.store_name}</span>
      <span className="tray-card-compare">{comparison(item)}</span>
      <span className="tray-card-state">{kept ? "✓ Kept" : "Rejected"}</span>
    </button>
  );
}

export function TrayCarousel({
  name,
  items,
  rejected,
  onChange,
}: {
  name: TrayName;
  items: TrayItem[];
  rejected: Set<number>;
  onChange: (next: Set<number>) => void;
}) {
  const { title, hint } = TRAY_TITLES[name];
  const keptCount = items.filter((i) => !rejected.has(i.deal_id)).length;

  const toggle = (dealId: number) => {
    const next = new Set(rejected);
    if (next.has(dealId)) next.delete(dealId);
    else next.add(dealId);
    onChange(next);
  };
  const setAll = (keep: boolean) => {
    const next = new Set(rejected);
    for (const i of items) {
      if (keep) next.delete(i.deal_id);
      else next.add(i.deal_id);
    }
    onChange(next);
  };

  return (
    <div className="tray">
      <div className="tray-header">
        <div>
          <h3 className="tray-title">
            {title}{" "}
            <span className="muted small">
              {keptCount} of {items.length} kept
            </span>
          </h3>
          <p className="muted small">{hint}</p>
        </div>
        <div className="tray-actions">
          <button type="button" className="button button-ghost" onClick={() => setAll(true)}>
            Keep all
          </button>
          <button type="button" className="button button-ghost" onClick={() => setAll(false)}>
            Reject all
          </button>
        </div>
      </div>
      {items.length === 0 ? (
        <p className="muted">Nothing in this tray this week.</p>
      ) : (
        <div className="tray-scroller" role="list" aria-label={`${title} tray`}>
          {items.map((item) => (
            <div role="listitem" key={item.deal_id}>
              <TrayCard
                item={item}
                kept={!rejected.has(item.deal_id)}
                onToggle={() => toggle(item.deal_id)}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function PantryChecklist({
  pantry,
  unchecked,
  onChange,
}: {
  pantry: PantryItem[];
  unchecked: Set<number>;
  onChange: (next: Set<number>) => void;
}) {
  if (pantry.length === 0) {
    return <p className="muted">No pantry staples are available for your diet.</p>;
  }
  const toggle = (id: number) => {
    const next = new Set(unchecked);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };
  return (
    <div className="pantry-grid">
      {pantry.map((p) => (
        <label key={p.product_id} className="toggle">
          <input
            type="checkbox"
            checked={!unchecked.has(p.product_id)}
            onChange={() => toggle(p.product_id)}
          />
          {p.name}
        </label>
      ))}
    </div>
  );
}
