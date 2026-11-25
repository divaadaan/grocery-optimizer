import { Link } from "react-router-dom";
import { Card, EmptyState, QueryError, SavingsCallout, Spinner } from "../../components/ui";
import { useCurrentUser } from "../user/UserContext";
import { useShoppingList } from "../../hooks/queries";
import { formatMoney, moneyToNumber } from "../../utils/format";
import type { ShoppingListItem } from "../../api/types";

/**
 * The shopping list for the user's latest meal plan (GET
 * /shopping-list/{user_id}, built from the recipes' deal citations). Each line
 * is ONE purchase of the cited deal, at its flyer price; "Recipes need" is the
 * amount the dishes call for, not a package count. Pantry staples are left off.
 */
export function ShoppingListPage() {
  const { user } = useCurrentUser();
  const list = useShoppingList(user?.user_id);

  if (!user) {
    return (
      <div className="page">
        <header className="page-head">
          <h1>Shopping list</h1>
        </header>
        <EmptyState
          title="Set up a profile first"
          action={
            <Link className="button button-primary" to="/setup">
              Set up profile
            </Link>
          }
        />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <span className="kicker muted">Shopping list</span>
        <h1>One trip per store</h1>
        <p>
          Every line is the exact flyer deal a recipe cited, priced once. Pantry staples you
          confirmed are left off.
        </p>
      </header>
      {list.isPending ? (
        <Spinner label="Loading shopping list…" />
      ) : list.isError ? (
        <QueryError error={list.error} stubTitle="Shopping lists" />
      ) : (
        <>
          <SavingsCallout savings={list.data.estimated_savings} />
          <Card>
            <div className="stats-row">
              <div className="stat">
                <span className="stat-value">{formatMoney(list.data.total_cost)}</span>
                <span className="stat-label">Total cost</span>
              </div>
              <div className="stat">
                <span className="stat-value">{list.data.items.length}</span>
                <span className="stat-label">Items</span>
              </div>
              <div className="stat">
                <span className="stat-value">{list.data.stores.length}</span>
                <span className="stat-label">Stores</span>
              </div>
            </div>
          </Card>
          <div className="receipts">
            {groupByStore(list.data.items).map(([store, items]) => (
              <section key={store} className="receipt" aria-label={store}>
                <h2>
                  {store}
                  <span className="num">{formatMoney(storeTotal(items))}</span>
                </h2>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Product</th>
                      <th>Recipes need</th>
                      <th className="num">Price</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item, i) => (
                      <tr key={i}>
                        <td>{String(item.product ?? "—")}</td>
                        <td className="muted">{String(item.quantity || "—")}</td>
                        <td className="num">{item.price != null ? formatMoney(item.price) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function storeTotal(items: ShoppingListItem[]): number {
  return items.reduce((sum, item) => sum + (item.price != null ? moneyToNumber(item.price) : 0), 0);
}

function groupByStore(items: ShoppingListItem[]): [string, ShoppingListItem[]][] {
  const groups = new Map<string, ShoppingListItem[]>();
  for (const item of items) {
    const store = item.store ?? "Store unknown";
    const group = groups.get(store) ?? [];
    group.push(item);
    groups.set(store, group);
  }
  return [...groups.entries()];
}
