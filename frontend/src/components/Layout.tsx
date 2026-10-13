import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpen,
  ClipboardCheck,
  FlaskConical,
  Headset,
  LayoutDashboard,
  LogOut,
  Menu,
  MessagesSquare,
  Monitor,
  Moon,
  RefreshCcw,
  ScrollText,
  Ticket,
  Sun,
  X,
} from "lucide-react";
import { api } from "../api";
import { isStaff, useAuth } from "../auth";
import { REPO_URL, isDemo } from "../demo/replay";
import type { Approval, Handoff } from "../types";
import { Logo, ROLE_TEXT, useMediaQuery, useTheme } from "./ui";

const TITLES: Record<string, string> = {
  dashboard: "Dashboard",
  chat: "Conversations",
  approvals: "Approval queue",
  handoffs: "Human support",
  tickets: "Tickets",
  knowledge: "Knowledge base",
  traces: "Traces",
  flywheel: "Data flywheel",
  evals: "Eval centre",
  audit: "Operation audit",
};

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  count?: number;
}

function DemoNotice() {
  return (
    <div className="demo-notice" role="note">
      Static demo: these pages were recorded from the real backend (offline model, fictional data). You can switch roles and replay the sample questions on the chat page; anything you submit will not take effect.
      <a href={REPO_URL} target="_blank" rel="noreferrer">Run the full version locally ↗</a>
    </div>
  );
}

export default function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const staff = isStaff(user);
  const [pending, setPending] = useState(0);
  const [waiting, setWaiting] = useState(0); // customers waiting for a person
  const [openTickets, setOpenTickets] = useState(0); // issue tickets promised a response
  const [theme, setTheme] = useTheme();
  // Below 780px the sidebar becomes an off-canvas drawer opened from the top bar.
  const compact = useMediaQuery("(max-width: 780px)");
  const [navOpen, setNavOpen] = useState(false);
  const drawerOpen = compact && navOpen;
  const toggleRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);

  useEffect(() => {
    if (!staff) return;
    const load = () => {
      api
        .get<Approval[]>("/api/approvals?status_filter=pending")
        .then((rows) => setPending(rows.length))
        .catch(() => undefined);
      api
        .get<Handoff[]>("/api/handoffs")
        .then((rows) => setWaiting(rows.filter((h) => h.position).length))
        .catch(() => undefined);
      api
        .get<{ status: string }[]>("/api/tickets")
        .then((rows) => setOpenTickets(rows.filter((t) => t.status === "open").length))
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, 8000);
    return () => window.clearInterval(timer);
  }, [staff, location.pathname]);

  // Dismissing the drawer returns focus to the menu button. This runs after the render
  // that removes `inert` from the main column; focusing earlier would be ignored.
  const closeNav = () => {
    restoreFocus.current = true;
    setNavOpen(false);
  };

  useEffect(() => {
    if (!drawerOpen) {
      if (restoreFocus.current) toggleRef.current?.focus();
      restoreFocus.current = false;
      return;
    }
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeNav();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const section = location.pathname.split("/")[1] || "chat";
  const groups: [string, NavItem[]][] = staff
    ? [
        ["Service", [
          { to: "/dashboard", label: "Dashboard", icon: <LayoutDashboard size={16} /> },
          { to: "/chat", label: "Conversation test bench", icon: <MessagesSquare size={16} /> },
          { to: "/approvals", label: "Approval queue", icon: <ClipboardCheck size={16} />, count: pending },
          { to: "/handoffs", label: "Human support", icon: <Headset size={16} />, count: waiting },
          { to: "/tickets", label: "Tickets", icon: <Ticket size={16} />, count: openTickets },
        ]],
        ["Knowledge and quality", [
          { to: "/knowledge", label: "Knowledge base", icon: <BookOpen size={16} /> },
          { to: "/evals", label: "Eval centre", icon: <FlaskConical size={16} /> },
          { to: "/flywheel", label: "Data flywheel", icon: <RefreshCcw size={16} /> },
        ]],
        ["Operations", [
          { to: "/traces", label: "Traces", icon: <Activity size={16} /> },
          ...(user?.role === "admin" ? [{ to: "/audit", label: "Operation audit", icon: <ScrollText size={16} /> }] : []),
        ]],
      ]
    : [["Service", [{ to: "/chat", label: "Customer support", icon: <MessagesSquare size={16} /> }]]];

  const nextTheme = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  const themeLabel = { system: "System", light: "Light", dark: "Dark" }[theme];

  return (
    <div className={`shell ${drawerOpen ? "nav-open" : ""}`}>
      {drawerOpen && <div className="scrim" onClick={closeNav} />}
      <aside
        id="app-nav"
        className="sidebar"
        aria-label="Navigation"
        {...(drawerOpen ? { role: "dialog", "aria-modal": true } : {})}
      >
        <div className="brand">
          <Logo />
          <span className="brand-name">LoopDesk</span>
          <button ref={closeRef} className="icon-btn nav-close" aria-label="Close navigation" onClick={closeNav}>
            <X size={18} />
          </button>
        </div>
        <div className="tenant-card">
          <div className="name">{user?.tenant.name}</div>
          <div className="meta">{user?.tenant.industry === "saas" ? "SaaS" : "Retail"} · demo tenant</div>
        </div>
        <nav className="nav" aria-label="Main navigation">
          {groups.map(([group, items]) => (
            <div key={group}>
              <div className="nav-group">{group}</div>
              {items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => (isActive ? "active" : "")}
                  onClick={() => setNavOpen(false)}
                >
                  {item.icon}
                  {item.label}
                  {item.count ? <span className="count">{item.count}<span className="sr-only"> waiting</span></span> : null}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="avatar me">{user?.name.slice(0, 1)}</span>
          <div style={{ minWidth: 0, lineHeight: 1.3 }}>
            <div className="ellipsis" style={{ fontSize: 13, fontWeight: 500 }}>{user?.name}</div>
            <div className="faint">{ROLE_TEXT[user?.role ?? ""]}</div>
          </div>
          <span className="spacer" />
          <button className="icon-btn" title="Sign out" aria-label="Sign out" onClick={logout}>
            <LogOut size={15} />
          </button>
        </div>
      </aside>
      <div className="main" inert={drawerOpen || undefined}>
        <header className="topbar">
          <div className="crumbs">
            <button
              ref={toggleRef}
              className="icon-btn nav-toggle"
              aria-label={staff && pending + waiting ? `Open navigation (${pending} approvals waiting, ${waiting} customers waiting for a person)` : "Open navigation"}
              aria-expanded={drawerOpen}
              aria-controls="app-nav"
              onClick={() => setNavOpen(true)}
            >
              <Menu size={18} />
              {staff && pending + waiting > 0 && <span className="toggle-dot" />}
            </button>
            <span className="ellipsis crumb-tenant">{user?.tenant.name}</span>
            <span className="crumb-tenant">/</span>
            <b className="ellipsis">{TITLES[section] ?? "LoopDesk"}</b>
          </div>
          <div className="row">
            <span className="badge">{isDemo ? "Static demo" : "Demo"}<span className="hide-compact"> · all data is fictional</span></span>
            <button className="icon-btn" title={`Theme: ${themeLabel} (click to change)`} aria-label={`Change theme, currently ${themeLabel}`} onClick={() => setTheme(nextTheme)}>
              {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
            </button>
          </div>
        </header>
        {isDemo && <DemoNotice />}
        <Outlet />
      </div>
    </div>
  );
}
