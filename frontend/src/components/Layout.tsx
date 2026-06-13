import { NavLink, Outlet, Link } from "react-router-dom";
import { useCurrentUser } from "../features/user/UserContext";
import { useHealth } from "../hooks/queries";

const NAV = [
  { to: "/", label: "Home" },
  { to: "/deals", label: "Deals" },
  { to: "/planner", label: "Meal planner" },
  { to: "/shopping-list", label: "Shopping list" },
];

export function Layout() {
  const { user } = useCurrentUser();

  return (
    <div className="layout">
      <header className="topbar">
        <Link to="/" className="brand">
          🥕 Grocery Optimizer
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
      </footer>
    </div>
  );
}

function HealthIndicator() {
  const health = useHealth();

  if (health.isError) {
    return <span className="health health-down">● API unreachable</span>;
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
            ● {name}
          </span>
        ))}
      <span className="muted">v{health.data.version}</span>
    </span>
  );
}
