import { Link } from "react-router-dom";
import { Card, EmptyState, QueryError, Spinner } from "../../components/ui";
import { useCurrentUser } from "../user/UserContext";
import { useShoppingList } from "../../hooks/queries";
import { formatMoney } from "../../utils/format";
import type { ShoppingListItem } from "../../api/types";

/**
 * The backend endpoint is a 501 stub (roadmap item 4); QueryError renders
 * that as a "coming soon" panel. The success path below is wired so the
 * page lights up the moment the Shopping Optimizer agent lands.
 */
export function ShoppingListPage() {
  const { user } = useCurrentUser();
  const list = useShoppingList(user?.user_id);

  if (!user) {
    return (
      <div className="page">
        <h1>Shopping list</h1>
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
      <h1>Shopping list</h1>
      {list.isPending ? (
        <Spinner label="Loading shopping list…" />
      ) : list.isError ? (
        <QueryError error={list.error} stubTitle="Shopping lists" />
      ) : (
        <>
          <Card title="Summary">
            <div className="stats-row">
              <div className="stat">
                <span className="stat-value">{formatMoney(list.data.total_cost)}</span>
                <span className="stat-label">Total cost</span>
              </div>
              <div className="stat">
                <span className="stat-value">{formatMoney(list.data.estimated_savings)}</span>
                <span className="stat-label">Estimated savings</span>
              </div>
              <div className="stat">
                <span className="stat-value">{list.data.stores.length}</span>
                <span className="stat-label">Stores</span>
              </div>
            </div>
          </Card>
          {groupByStore(list.data.items).map(([store, items]) => (
            <Card key={store} title={store}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Price</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, i) => (
                    <tr key={i}>
                      <td>{String(item.product ?? "—")}</td>
                      <td>{String(item.quantity ?? "—")}</td>
                      <td>{item.price != null ? formatMoney(item.price) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          ))}
        </>
      )}
    </div>
  );
}

function groupByStore(items: ShoppingListItem[]): [string, ShoppingListItem[]][] {
  const groups = new Map<string, ShoppingListItem[]>();
  for (const item of items) {
    const store = item.store ?? "Any store";
    const group = groups.get(store) ?? [];
    group.push(item);
    groups.set(store, group);
  }
  return [...groups.entries()];
}
