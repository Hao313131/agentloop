import { Link } from "react-router-dom";
import { Download } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Kpi, TOOL_TEXT, ms, pct, useLoad, TIER_TEXT } from "../components/ui";

interface Overview {
  window_days: number;
  timezone: string;
  runs: number;
  status: Record<string, number>;
  conversations: number;
  resolution_rate: number | null; // per conversation: no handoff, no refusal, no negative feedback
  handoff_rate: number | null;
  refusal_rate: number | null;
  pending_approvals: number;
  handoff_queue: { waiting: number; in_progress: number; longest_wait_s: number; handoffs: number; avg_first_response_s: number | null };
  knowledge_conflicts: { open: number; oldest_open_s: number; handoffs: number };
  tickets: { open: number; overdue: number };
  failures: { runs: number; rate: number | null; last_15m: number; kinds: Record<string, number>; last_error: string | null; last_kind: string | null };
  latency_p50_ms: number;
  latency_p95_ms: number;
  tokens: number;
  cost: number;
  avg_evidence_coverage: number | null;
  verdicts: Record<string, number>;
  tools: Record<string, number>;
  route_tiers: Record<string, number>;
  cache_hits: number;
  cache_hit_rate: number | null;
  prompt_versions: Record<string, { runs: number; handoff_rate: number; blocked: number; avg_evidence_coverage: number | null; avg_tokens: number }>;
  feedback: Record<string, number>;
  daily: { day: string; runs: number; handoff: number }[];
  last_eval: { id: number; pass_rate: number; total: number } | null;
}

function Bars({ data, labels }: { data: Record<string, number>; labels?: Record<string, string> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (!entries.length) return <Empty>No data yet</Empty>;
  return (
    <div className="stack">
      {entries.map(([k, v]) => (
        <div key={k}>
          <div className="row small"><span>{labels?.[k] ?? k}</span><span className="spacer" /><span className="mono">{v}</span></div>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / max) * 100}%` }} /></div>
        </div>
      ))}
    </div>
  );
}

function DailyChart({ daily }: { daily: Overview["daily"] }) {
  if (!daily.length) return <Empty>No data yet</Empty>;
  const max = Math.max(1, ...daily.map((d) => d.runs));
  const w = 100 / daily.length;
  return (
    <svg viewBox="0 0 100 60" preserveAspectRatio="none" style={{ width: "100%", height: 160 }} role="img" aria-label="Runs per day">
      {daily.map((d, i) => {
        const h = (d.runs / max) * 50;
        const hh = (d.handoff / max) * 50;
        return (
          <g key={d.day}>
            <rect x={i * w + w * 0.2} y={52 - h} width={w * 0.6} height={h} fill="var(--primary)" rx={0.8}><title>{`${d.day}: ${d.runs} runs`}</title></rect>
            <rect x={i * w + w * 0.2} y={52 - hh} width={w * 0.6} height={hh} fill="var(--info)" rx={0.8}><title>{`${d.day}: ${d.handoff} handed to a person`}</title></rect>
            <text x={i * w + w / 2} y={59} fontSize={3.2} textAnchor="middle" fill="var(--text-3)">{d.day}</text>
          </g>
        );
      })}
    </svg>
  );
}

// Why runs hit an error (N-037). Customers were served either way (quoted policy or a person, N-031).
const FAILURE_KIND: Record<string, string> = {
  unavailable: "model service unavailable", rejected: "request rejected (usually a config or parameter issue)", timeout: "timed out",
  breaker: "circuit breaker open", disconnected: "customer disconnected", interrupted: "process interrupted", other: "other",
};

// The dashboard's days are the tenant's calendar days (N-024); say which zone that is.
function zoneLabel(zone: string) {
  return zone === "Australia/Sydney" ? "Sydney time" : zone;
}

const EXPORTS: [string, string][] = [
  ["conversations", "Full conversations"],
  ["tickets", "Tickets"],
  ["handoffs", "Human support"],
  ["runs", "Run records"],
  ["feedback", "Feedback"],
  ["approvals", "Approvals"],
];

const duration = (seconds: number | null | undefined) =>
  seconds == null ? "—" : seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)}h`;

export default function DashboardPage() {
  const { data, error } = useLoad(() => api.get<Overview>("/api/metrics/overview?days=7"));
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>Real conversations from the last 7 days (calendar days, including today), excluding eval runs. Costs are estimated from the configured model prices; the offline model's price is shown as an example only.</p>
        </div>
        <div className="row row-wrap" aria-label="Export the last 30 days of data">
          <span className="faint">Export last 30 days (CSV)</span>
          {EXPORTS.map(([kind, label]) => (
            <button key={kind} className="btn btn-sm" onClick={() => api.download(`/api/export/${kind}.csv?days=30`, `loopdesk-${kind}.csv`)}>
              <Download size={14} aria-hidden="true" /> {label}
            </button>
          ))}
        </div>
      </div>
      <ErrorBanner error={error} />
      {!data ? <Empty>Loading…</Empty> : (
        <>
          {data.failures.last_15m > 0 && (
            <div className="error-banner" role="alert">
              {`${data.failures.last_15m} runs errored in the last 15 minutes (`
                + Object.entries(data.failures.kinds).map(([kind, n]) => `${FAILURE_KIND[kind] ?? kind} ${n}`).join(", ")
                + `); customers were served either way (a reply quoting the source, or a person), and those conversations are not counted as self-resolved. `
                + `Most recent: ${FAILURE_KIND[data.failures.last_kind ?? ""] ?? "other"} (${data.failures.last_error ?? "—"}).`}
              <Link to="/traces">View traces →</Link>
            </div>
          )}
          {data.tickets.overdue > 0 && (
            <div className="error-banner" role="alert">
              {`${data.tickets.overdue} tickets have passed the response time promised to the customer (${data.tickets.open} open in total).`}
              <Link to="/tickets">Go and handle them →</Link>
            </div>
          )}
          {data.knowledge_conflicts.open > 0 && (
            <div className="notice" role="status">
              {`The knowledge base has ${data.knowledge_conflicts.open} conflicts to review (longest open ${duration(data.knowledge_conflicts.oldest_open_s)}). `
                + `Until they are reviewed, questions that touch both versions are handed to a person — ${data.knowledge_conflicts.handoffs} times in the last ${data.window_days} days.`}
              <Link to="/knowledge">Go and review →</Link>
            </div>
          )}
          <div className="grid grid-4">
            <Kpi label="Agent runs" value={data.runs} sub={`Resolved ${data.status.completed ?? 0} · To a person ${data.status.handoff ?? 0} · Cache hits ${pct(data.cache_hit_rate)}`} />
            <Kpi label="Self-resolution rate (by conversation)" value={pct(data.resolution_rate)}
              sub={`${data.conversations} conversations · To a person ${pct(data.handoff_rate)} · Refusals ${pct(data.refusal_rate)}`} />
            <Kpi label="Average evidence coverage" value={pct(data.avg_evidence_coverage)}
              sub={`Sufficient ${data.verdicts.pass ?? 0} · Partial ${data.verdicts.warn ?? 0} · Conflict ${data.verdicts.blocked ?? 0}`} />
            <Kpi label="Pending approvals / Waiting for a person"
              value={<><Link to="/approvals">{data.pending_approvals}</Link> / <Link to="/handoffs">{data.handoff_queue.waiting}</Link></>}
              sub={`In progress ${data.handoff_queue.in_progress} · Longest wait ${duration(data.handoff_queue.longest_wait_s)} · First response ${duration(data.handoff_queue.avg_first_response_s)}`} />
            <Kpi label="Latency p50 / p95" value={`${ms(data.latency_p50_ms)} / ${ms(data.latency_p95_ms)}`}
              sub={`Errors ${pct(data.failures.rate)} (${data.failures.runs} runs) · ${data.failures.last_15m} in the last 15 min`} />
            <Kpi label="Tokens / Est. cost" value={data.tokens.toLocaleString()} sub={`$${data.cost.toFixed(4)}`} />
            <Kpi label="User feedback" value={(data.feedback.up ?? 0) + (data.feedback.down ?? 0) + (data.feedback.correction ?? 0)}
              sub={`Helpful ${data.feedback.up ?? 0} · Not helpful ${data.feedback.down ?? 0} · Corrections ${data.feedback.correction ?? 0}`} />
            <Kpi label="Latest regression eval" value={data.last_eval ? pct(data.last_eval.pass_rate, 1) : "—"}
              sub={data.last_eval ? <Link to="/evals">Run #{data.last_eval.id} · {data.last_eval.total} cases</Link> : <Link to="/evals">Run one</Link>} />
          </div>
          <div className="grid grid-3">
            <div className="card">
              <div className="card-head"><h3>Runs per day</h3><span className="faint">Blue: all · Purple: to a person · days split by {zoneLabel(data.timezone)}</span></div>
              <div className="card-pad"><DailyChart daily={data.daily} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>Tool usage</h3></div>
              <div className="card-pad"><Bars data={data.tools} labels={TOOL_TEXT} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>Model routing</h3><span className="faint">Repeat questions hit the cache; simple ones take the fast model</span></div>
              <div className="card-pad"><Bars data={data.route_tiers} labels={TIER_TEXT} /></div>
            </div>
          </div>
          <div className="card table-wrap">
            <div className="card-head"><h3>Prompt version comparison</h3><span className="faint">Traffic is split per conversation during a rollout; compare how each version performs in real conversations</span></div>
            <table className="table">
              <thead><tr><th>Version</th><th>Runs</th><th>Handoff rate</th><th>Evidence conflicts</th><th>Avg. evidence coverage</th><th>Avg. tokens</th></tr></thead>
              <tbody>
                {Object.entries(data.prompt_versions).map(([version, v]) => (
                  <tr key={version}>
                    <td className="mono">{version}</td>
                    <td>{v.runs}</td>
                    <td>{pct(v.handoff_rate)}</td>
                    <td>{v.blocked}</td>
                    <td>{pct(v.avg_evidence_coverage)}</td>
                    <td className="mono">{v.avg_tokens}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
