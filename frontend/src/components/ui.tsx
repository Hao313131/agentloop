import { useCallback, useEffect, useId, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { X } from "lucide-react";

export const TIER_TEXT: Record<string, string> = { fast: "Fast model", strong: "Strong model", cache: "Answer cache", rule: "Rules (no model call)" };
export const tierText = (tier?: string | null) => TIER_TEXT[tier ?? ""] ?? tier ?? "—";

export const ROLE_TEXT: Record<string, string> = { admin: "Admin", agent: "Support agent", customer: "Customer" };

export const TOOL_TEXT: Record<string, string> = {
  search_knowledge: "Search the knowledge base",
  get_order: "Look up an order",
  track_shipment: "Track a shipment",
  request_refund: "Request a refund",
  create_ticket: "Create a ticket",
  handoff_to_human: "Hand off to a person",
};

const RUN_STATUS: Record<string, [string, string]> = {
  completed: ["Resolved", "badge-success"],
  handoff: ["With a person", "badge-info"],
  waiting_approval: ["Awaiting approval", "badge-warning"],
  running: ["Running", "badge-primary"],
  failed: ["Failed", "badge-danger"],
  pending: ["Pending approval", "badge-warning"],
  approved: ["Approved", "badge-success"],
  rejected: ["Rejected", "badge-danger"],
  proposed: ["Proposed", "badge-warning"],
  applied: ["Published", "badge-success"],
  candidate: ["Candidate", "badge-warning"],
  active: ["Active", "badge-success"],
  archived: ["Archived", ""],
};

export const CHANNEL_TEXT: Record<string, string> = { web: "Web", whatsapp: "WhatsApp", slack: "Slack", api: "API", eval: "Eval" };

export function StatusBadge({ status }: { status: string }) {
  const [text, cls] = RUN_STATUS[status] ?? [status, ""];
  return <span className={`badge ${cls}`}>{text}</span>;
}

export function VerdictBadge({ verdict, coverage }: { verdict?: string | null; coverage?: number | null }) {
  if (!verdict) return <span className="faint">—</span>;
  const map: Record<string, [string, string]> = {
    pass: ["Evidence sufficient", "badge-success"],
    warn: ["Partly unsupported", "badge-warning"],
    blocked: ["Evidence conflict", "badge-danger"],
  };
  const [text, cls] = map[verdict] ?? [verdict, ""];
  return (
    <span className={`badge ${cls}`} title="Sentence-by-sentence evidence check result">
      {text}
      {coverage != null && ` · ${Math.round(coverage * 100)}%`}
    </span>
  );
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="card modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="card-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="card-pad stack">{children}</div>
        {footer && <div className="card-head" style={{ borderTop: "1px solid var(--border)", borderBottom: "none", justifyContent: "flex-end" }}>{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const titleId = useId();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="card-head" style={{ position: "sticky", top: 0, background: "var(--surface)", zIndex: 1 }}>
          <div id={titleId} style={{ minWidth: 0 }}>{title}</div>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="card-pad">{children}</div>
      </aside>
    </>
  );
}

export function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json mono">{JSON.stringify(value, null, 2)}</pre>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBanner({ error }: { error: string | null }) {
  return error ? <div className="error-banner">{error}</div> : null;
}

export const pct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
export const ms = (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
/** Australian dollars, the tenant's trading currency: grouped the way an Australian invoice writes it. */
export const aud = (v: unknown) =>
  `$${Number(v ?? 0).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const pad = (n: number) => String(n).padStart(2, "0");

/** Friendly timestamps: just now / 5 min ago / Today 14:05 / Yesterday 09:12 / 10-08 14:05. */
export function time(iso?: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const seconds = (now.getTime() - d.getTime()) / 1000;
  if (seconds >= 0 && seconds < 60) return "just now";
  if (seconds >= 0 && seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d.getTime() >= startOfToday) return `Today ${hm}`;
  if (d.getTime() >= startOfToday - 86400000) return `Yesterday ${hm}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

export function fullTime(iso?: string | null) {
  return iso ? new Date(iso).toLocaleString("en-AU", { hour12: false }) : "—";
}

export function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect width="24" height="24" rx="5" fill="var(--primary)" />
      <path d="M7.5 12a4.5 4.5 0 1 1 4.5 4.5" stroke="#fff" strokeWidth="2" fill="none" strokeLinecap="round" />
      <path d="M12 16.5h-3" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
      <circle cx="12" cy="12" r="1.6" fill="#fff" />
    </svg>
  );
}

type Theme = "system" | "light" | "dark";
const THEME_KEY = "agentloop.theme";

function applyTheme(theme: Theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
}

export function initTheme() {
  try {
    applyTheme((localStorage.getItem(THEME_KEY) as Theme) || "system");
  } catch {
    /* storage unavailable: follow the system */
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem(THEME_KEY) as Theme) || "system";
    } catch {
      return "system";
    }
  });
  const update = useCallback((t: Theme) => {
    setTheme(t);
    applyTheme(t);
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch {
      /* ignore */
    }
  }, []);
  return [theme, update];
}

/** Keyboard support for a clickable element that cannot be a <button> (table rows, list
 * items): focusable with Tab, activated with Enter or Space like a button. */
export function pressable(onPress: () => void) {
  return {
    tabIndex: 0,
    onClick: onPress,
    onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => {
      if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      onPress();
    },
  };
}

export const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Track a CSS media query, e.g. the 780px breakpoint where the sidebar becomes a drawer. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Load data with loading/error state and a reload function. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await run());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [run]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}
