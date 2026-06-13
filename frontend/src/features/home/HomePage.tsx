import { Link } from "react-router-dom";
import { Badge, Card } from "../../components/ui";
import { useCurrentUser } from "../user/UserContext";
import { useApiInfo } from "../../hooks/queries";
import { formatMoney, humanize } from "../../utils/format";

export function HomePage() {
  const { user } = useCurrentUser();
  const info = useApiInfo();

  return (
    <div className="page">
      <div className="hero">
        <h1>Grocery Optimizer</h1>
        <p className="muted">
          AI meal plans built around what's actually on sale near you. A Chef
          agent groups discounted ingredients for reuse, SousChefs draft recipes
          in parallel, and a Nutritionist signs off on every meal.
        </p>
      </div>

      {user ? (
        <Card
          title="Your profile"
          actions={
            <Link className="button button-ghost" to="/setup">
              Edit
            </Link>
          }
        >
          <div className="stats-row">
            <div className="stat">
              <span className="stat-value">{user.postal_code}</span>
              <span className="stat-label">Postal code</span>
            </div>
            <div className="stat">
              <span className="stat-value">{formatMoney(user.budget)}</span>
              <span className="stat-label">Budget</span>
            </div>
            <div className="stat">
              <span className="stat-value">{user.household_size}</span>
              <span className="stat-label">Household</span>
            </div>
          </div>
          {user.dietary_restrictions.length > 0 && (
            <p>
              {user.dietary_restrictions.map((restriction) => (
                <Badge key={restriction} tone="accent">
                  {humanize(restriction)}
                </Badge>
              ))}
            </p>
          )}
        </Card>
      ) : (
        <Card title="Get started">
          <p className="muted">
            Create a profile with your postal code, budget, and dietary
            restrictions to start planning.
          </p>
          <Link className="button button-primary" to="/setup">
            Set up profile
          </Link>
        </Card>
      )}

      <div className="grid-cards">
        <FeatureLink
          to="/deals"
          title="Browse deals"
          description="See what's on sale at stores near you."
        />
        <FeatureLink
          to="/planner"
          title="Plan meals"
          description="Generate a week of recipes optimized for the current deals."
        />
        <FeatureLink
          to="/shopping-list"
          title="Shopping list"
          description="A consolidated list across stores, priced from the deals."
        />
      </div>

      {info.data && (
        <p className="muted small">
          Models — chef: {info.data.features.ollama_models.chef} · sous chef:{" "}
          {info.data.features.ollama_models.sous_chef} · nutritionist:{" "}
          {info.data.features.ollama_models.nutritionist} ({info.data.environment})
        </p>
      )}
    </div>
  );
}

function FeatureLink({
  to,
  title,
  description,
}: {
  to: string;
  title: string;
  description: string;
}) {
  return (
    <Link to={to} className="feature-link">
      <h3>{title}</h3>
      <p className="muted">{description}</p>
    </Link>
  );
}
