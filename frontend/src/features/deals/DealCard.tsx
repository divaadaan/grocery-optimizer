import { Badge } from "../../components/ui";
import { daysUntil, formatDate, formatMoney } from "../../utils/format";
import type { DealInfo } from "../../api/types";

export function DealCard({ deal }: { deal: DealInfo }) {
  const days = daysUntil(deal.valid_until);
  const endsSoon = days !== null && days >= 0 && days <= 3;
  const endsLabel = days === 0 ? "Ends today" : days === 1 ? "Ends tomorrow" : `${days} days left`;

  return (
    <article className="deal-card">
      <div className="deal-card-top">
        <h3>{deal.product_name}</h3>
        <Badge tone="success">−{deal.discount_percentage}%</Badge>
      </div>
      {deal.brand && <p className="muted">{deal.brand}</p>}
      <p className="deal-price">
        <strong>{formatMoney(deal.sale_price)}</strong>
        <s className="muted">{formatMoney(deal.regular_price)}</s>
        {deal.unit && <span className="muted"> / {deal.unit}</span>}
      </p>
      <footer className="deal-card-footer">
        <span>
          {deal.store_name}
          {deal.chain && deal.chain !== deal.store_name ? ` (${deal.chain})` : ""}
        </span>
        {deal.category && <Badge>{deal.category}</Badge>}
      </footer>
      <p className="deal-expiry small">
        <span className="muted">Until {formatDate(deal.valid_until)}</span>
        {endsSoon && <Badge tone="warning">{endsLabel}</Badge>}
      </p>
    </article>
  );
}
