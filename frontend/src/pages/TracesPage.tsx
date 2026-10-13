import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Json, Kpi, ROLE_TEXT, StatusBadge, VerdictBadge, ms, pressable, time, useLoad, tierText } from "../components/ui";
import type { EvidenceItem, RunRow, SpanRow } from "../types";

const KIND_TEXT: Record<string, string> = {
  guardrail: "Guardrail", router: "Routing", llm: "Model", tool: "Tool", verify: "Verification", approval: "Approval", notify: "Notification",
  cache: "Cache",
  rewrite: "Rewrite",
};

interface RunDetail extends RunRow {
  spans: SpanRow[];
  evidence: EvidenceItem[];
  approvals: { id: number; status: string; tool_name: string; reason: string }[];
}

function RunDetailView({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const { data, error } = useLoad(() => api.get<RunDetail>(`/api/runs/${runId}`), [runId]);
  const [open, setOpen] = useState<number | null>(null);
  if (error) return <ErrorBanner error={error} />;
  if (!data) return <Empty>Loading…</Empty>;
  const maxLatency = Math.max(1, ...data.spans.map((s) => s.latency_ms));
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row">
        <button className="btn btn-sm" onClick={() => navigate("/traces")}><ArrowLeft size={14} /> Back to the list</button>
        <span className="mono faint">trace {data.id}</span>
        {data.is_eval && <span className="badge badge-info">Eval run</span>}
      </div>
      <div className="grid grid-4">
        <Kpi label="Status" value={<StatusBadge status={data.status} />} sub={`${data.steps} steps · ${tierText(data.route_tier)}${data.route_tier === "cache" ? "" : ` ${data.model}`}`} />
        <Kpi label="End-to-end time" value={ms(data.latency_ms)} sub={time(data.created_at)} />
        <Kpi label="Tokens" value={(data.prompt_tokens + data.completion_tokens).toLocaleString()} sub={`In ${data.prompt_tokens} / out ${data.completion_tokens} · $${data.cost.toFixed(5)}`} />
        <Kpi label="Evidence check" value={<VerdictBadge verdict={data.verdict} coverage={data.evidence_coverage} />} sub="Sentence-by-sentence coverage" />
      </div>
      <div className="grid grid-2">
        <div className="card card-pad stack">
          <div className="faint">User input (already redacted) · {data.user?.name} ({ROLE_TEXT[data.user?.role ?? ""]})</div>
          <div>{data.input_text}</div>
        </div>
        <div className="card card-pad stack">
          <div className="faint">Final answer</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{data.output_text}</div>
          {data.error && <ErrorBanner error={data.error} />}
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h3>Execution timeline ({data.spans.length} spans)</h3><span className="faint">Click a row to expand its input and output</span></div>
        {data.spans.map((s) => (
          <div key={s.id}>
            <div className="span-row" role="button" aria-expanded={open === s.id} {...pressable(() => setOpen(open === s.id ? null : s.id))}>
              <span className={`badge ${s.status === "ok" ? "" : s.status === "rejected" ? "badge-warning" : "badge-danger"}`}>{KIND_TEXT[s.kind] ?? s.kind}</span>
              <span className="ellipsis"><b>{s.name}</b> {s.prompt_tokens + s.completion_tokens > 0 && <span className="faint">· {s.prompt_tokens + s.completion_tokens} tokens</span>}</span>
              <div className="bar-track"><div className={`span-bar kind-${s.kind}`} style={{ width: `${(s.latency_ms / maxLatency) * 100}%` }} /></div>
              <span className="mono faint">{ms(s.latency_ms)}</span>
            </div>
            {open === s.id && (
              <div className="grid grid-2" style={{ padding: 12, borderBottom: "1px solid var(--border)" }}>
                <div><div className="faint">input</div><Json value={s.input} /></div>
                <div><div className="faint">output</div><Json value={s.output} /></div>
              </div>
            )}
          </div>
        ))}
      </div>
      {data.evidence.length > 0 && (
        <div className="card">
          <div className="card-head"><h3>Evidence list</h3><span className="faint">The [n] markers in the answer match the numbers here</span></div>
          <div className="card-pad stack">
            {data.evidence.map((e) => (
              <div key={e.ref} className="json small">
                <b>[{e.ref}] {e.kind === "kb" ? e.title : `Tool: ${e.title}`}</b> <span className="faint">{e.meta.heading}</span>
                <div className="mono" style={{ whiteSpace: "pre-wrap" }}>{e.text}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function TracesPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [status, setStatus] = useState("");
  const [includeEval, setIncludeEval] = useState(false);
  const { data, error } = useLoad(
    () => api.get<{ items: RunRow[] }>(`/api/runs?limit=100${status ? `&status=${status}` : ""}${includeEval ? "&include_eval=true" : ""}`),
    [status, includeEval],
  );
  if (runId) return <div className="page"><RunDetailView runId={runId} /></div>;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Traces</h1>
          <p>Every agent run is one trace: routing, model calls, tools, approvals and the evidence check can all be replayed. Phone numbers and other personal details in the logs are already masked.</p>
        </div>
        <div className="row">
          <select className="select" style={{ width: 140 }} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            <option value="completed">Resolved</option>
            <option value="handoff">With a person</option>
            <option value="waiting_approval">Awaiting approval</option>
            <option value="failed">Failed</option>
          </select>
          <label className="row small muted"><input type="checkbox" checked={includeEval} onChange={(e) => setIncludeEval(e.target.checked)} /> Include eval runs</label>
        </div>
      </div>
      <ErrorBanner error={error} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>Time</th><th>User</th><th>Input</th><th>Status</th><th>Model</th><th>Latency</th><th>Tokens</th><th>Evidence</th></tr></thead>
          <tbody>
            {data?.items.map((r) => (
              <tr key={r.id} className="clickable" {...pressable(() => navigate(`/traces/${r.id}`))}>
                <td className="faint nowrap">{time(r.created_at)}</td>
                <td className="nowrap">{r.user?.name}</td>
                <td style={{ maxWidth: 380 }}><div className="ellipsis">{r.input_text}</div></td>
                <td><StatusBadge status={r.status} /></td>
                <td className="nowrap"><span className={`badge ${r.route_tier === "strong" ? "badge-info" : r.route_tier === "cache" ? "badge-success" : ""}`}>{r.route_tier === "strong" ? "Strong" : r.route_tier === "cache" ? "Cache" : r.route_tier === "rule" ? "Rules" : "Fast"}</span> <span className="faint">{r.model}</span></td>
                <td className="mono">{ms(r.latency_ms)}</td>
                <td className="mono">{r.prompt_tokens + r.completion_tokens}</td>
                <td><VerdictBadge verdict={r.verdict} coverage={r.evidence_coverage} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data?.items.length === 0 && <Empty>No runs yet. Ask a question in the <Link to="/chat">conversation test bench</Link> first.</Empty>}
      </div>
    </div>
  );
}
