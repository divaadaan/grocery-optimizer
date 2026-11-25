import { Link } from "react-router-dom";
import { Badge } from "../../components/ui";
import { useCurrentUser } from "../user/UserContext";
import { useApiInfo } from "../../hooks/queries";
import { humanize } from "../../utils/format";

const STEPS = [
  {
    title: "Trays",
    body: "This week's flyers, filtered by your diet and sorted into anchors, sides and stock-up buys, each compared across chains.",
  },
  {
    title: "Your call",
    body: "Reject anything you don't want and confirm what's already in your pantry. Nothing is planned from ingredients you didn't keep.",
  },
  {
    title: "Dishes",
    body: "A Chef agent plans one dish per meal around an anchor; SousChefs write each recipe, citing the exact deal behind every ingredient.",
  },
];

export function HomePage() {
  const { user } = useCurrentUser();
  const info = useApiInfo();

  return (
    <div className="page">
      <section className="hero">
        <span className="kicker muted">Weekly meal planning from grocery flyers</span>
        <h1>
          What can you cook from <em>this week's sales</em>?
        </h1>
        <p>
          Meal plans built from the grocery deals near you, within your dietary
          restrictions. Every ingredient points back to a real flyer price, and
          your diet is checked against curated product data, never guessed by
          a model.
        </p>
        <div className="hero-actions">
          {user ? (
            <Link className="button button-primary button-large" to="/planner">
              Plan this week's meals
            </Link>
          ) : (
            <Link className="button button-primary button-large" to="/setup">
              Create a profile
            </Link>
          )}
          <Link className="button button-ghost" to="/deals">
            Browse the deals
          </Link>
        </div>
      </section>

      <ol className="steps">
        {STEPS.map((step, i) => (
          <li key={step.title}>
            <span className="step-no">0{i + 1}</span>
            <h3>{step.title}</h3>
            <p>{step.body}</p>
          </li>
        ))}
      </ol>

      {user && (
        <section className="profile-strip" aria-label="Your profile">
          <div className="stats-row">
            <div className="stat">
              <span className="stat-value">{user.postal_code}</span>
              <span className="stat-label">Postal code</span>
            </div>
            <div className="stat">
              <span className="stat-value">{user.household_size}</span>
              <span className="stat-label">Household</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                {user.dietary_restrictions.length === 0
                  ? "None"
                  : user.dietary_restrictions.map((r) => (
                      <Badge key={r} tone="success">
                        {humanize(r)}
                      </Badge>
                    ))}
              </span>
              <span className="stat-label">Dietary restrictions</span>
            </div>
          </div>
          <Link className="button" to="/setup">
            Edit profile
          </Link>
        </section>
      )}

      <nav className="home-links" aria-label="Sections">
        <FeatureLink to="/deals" title="Deals" description="Everything on sale near you this week." />
        <FeatureLink to="/planner" title="Planner" description="Review the trays, then plan your meals." />
        <FeatureLink to="/recipes" title="Recipes" description="Every dish you've generated, saved." />
        <FeatureLink
          to="/shopping-list"
          title="Shopping list"
          description="Your latest plan, grouped by store."
        />
      </nav>

      {info.data && (
        <p className="models-line">
          Chef {info.data.features.models?.chef ?? "?"} · SousChef{" "}
          {info.data.features.models?.sous_chef ?? "?"} · labeller{" "}
          {info.data.features.models?.labeller ?? "?"}
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
