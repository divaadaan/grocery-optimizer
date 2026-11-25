import { NavLink, Outlet, Link } from "react-router-dom";
import { useCurrentUser } from "../features/user/UserContext";
import { useHealth } from "../hooks/queries";

const NAV = [
  { to: "/", label: "Home" },
  { to: "/deals", label: "Deals" },
  { to: "/planner", label: "Meal planner" },
  { to: "/recipes", label: "Saved recipes" },
  { to: "/shopping-list", label: "Shopping list" },
];

export function Layout() {
  const { user } = useCurrentUser();

  return (
    <div className="layout">
      <header className="topbar">
        <Link to="/" className="brand">
          <BrandMark />
          Grocery Optimizer
        </Link>
        <nav className="nav">
          {NAV.map(({ to, label }) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
        <Link to="/setup" className="user-chip">
          {user ? user.email : "Sign up"}
        </Link>
      </header>
      <main className="content">
        <Outlet />
      </main>
      <footer className="footer">
        <HealthIndicator />
        <span>Prices from this week's flyers · cost is reported, never a limit</span>
      </footer>
    </div>
  );
}

/** A shelf tag with a punched hole: the app's mark. */
function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M3 12.6V4.5A1.5 1.5 0 0 1 4.5 3h8.1a1.5 1.5 0 0 1 1.06.44l7 7a1.5 1.5 0 0 1 0 2.12l-8.1 8.1a1.5 1.5 0 0 1-2.12 0l-7-7A1.5 1.5 0 0 1 3 12.6Z"
        fill="currentColor"
      />
      <circle cx="8" cy="8" r="1.8" fill="var(--color-bg)" />
    </svg>
  );
}

function HealthIndicator() {
  const health = useHealth();

  if (health.isError) {
    return <span className="health health-down">API unreachable</span>;
  }
  if (!health.data) return null;

  const services: [string, string | null][] = [
    ["db", health.data.database],
    ["redis", health.data.redis],
    ["ollama", health.data.ollama],
  ];

  return (
    <span className="health">
      {services
        .filter(([, status]) => status !== null)
        .map(([name, status]) => (
          <span key={name} className={status === "healthy" ? "health-ok" : "health-down"}>
            {name}
          </span>
        ))}
      <span className="muted">v{health.data.version}</span>
    </span>
  );
}
