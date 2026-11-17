import { NavLink, Outlet, useLocation } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { Icon } from "./components";
import { ErrorBoundary } from "./ErrorBoundary";
import { useAuth } from "./auth";
import type { Dashboard } from "./types";

const NAV = [
  { to: "/", label: "Home", icon: "home", end: true },
  { to: "/library", label: "Library", icon: "library" },
  { to: "/add", label: "Add", icon: "plus" },
  { to: "/queue", label: "Queue", icon: "queue" },
  { to: "/settings", label: "Settings", icon: "settings" },
] as const;

export default function Layout() {
  const { logout, user } = useAuth();
  const { pathname } = useLocation();
  const { data } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/api/dashboard"),
    refetchInterval: 15000,
  });
  const failed = data?.jobs.failed ?? 0;
  const reviews = data?.open_reviews ?? 0;
  const approvals = data?.pending_approvals ?? 0;

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Main">
        <div className="brand">
          <img src="/favicon.svg" alt="" width={28} height={28} /> WebDL
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={"end" in n ? n.end : false}>
            <Icon name={n.icon} />
            {n.label}
            {n.to === "/queue" && failed > 0 && <span className="badge bad" style={{ marginLeft: "auto" }}>{failed}</span>}
          </NavLink>
        ))}
        <NavLink to="/stats">
          <Icon name="chart" />
          Statistics
        </NavLink>
        <NavLink to="/approvals">
          <Icon name="inbox" />
          Approvals
          {approvals > 0 && <span className="badge accent" style={{ marginLeft: "auto" }}>{approvals}</span>}
        </NavLink>
        <NavLink to="/review">
          <Icon name="review" />
          Review
          {reviews > 0 && <span className="badge warn" style={{ marginLeft: "auto" }}>{reviews}</span>}
        </NavLink>
        <NavLink to="/about">
          <Icon name="info" />
          Info
        </NavLink>
        <div className="spacer" />
        <div className="small muted" style={{ padding: "0 12px 6px" }}>Signed in as {user?.username}</div>
        <button className="btn ghost" style={{ justifyContent: "flex-start" }} onClick={logout}>
          <Icon name="logout" /> Sign out
        </button>
      </nav>

      <main className="main" id="main">
        {/* keyed by page: navigating elsewhere clears an error */}
        <ErrorBoundary key={pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>

      <nav className="bottom-nav" aria-label="Main mobile">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={"end" in n ? n.end : false}>
            <Icon name={n.icon} />
            <span>{n.label}</span>
            {n.to === "/queue" && failed > 0 && <span className="dot" aria-label={`${failed} failed`}>{failed}</span>}
            {n.to === "/settings" && reviews > 0 && <span className="dot" aria-label={`${reviews} to review`}>{reviews}</span>}
            {n.to === "/add" && approvals > 0 && <span className="dot" aria-label={`${approvals} to approve`}>{approvals}</span>}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
