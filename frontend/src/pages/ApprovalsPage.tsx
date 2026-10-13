import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, X } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, ROLE_TEXT, StatusBadge, TOOL_TEXT, aud, time, useLoad } from "../components/ui";
import type { Approval } from "../types";

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: (summary: string) => void }) {
  const { user } = useAuth();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const own = approval.requested_by?.id === user?.id;
  const needsAdmin = approval.required_role === "admin" && user?.role !== "admin";

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ final: { text: string }; delivery: { sent: boolean; error?: string } | null }>(
        `/api/approvals/${approval.id}/decide`, { decision, comment });
      const text = r.final?.text ?? "Handled";
      setResult(text);
      const notice = r.delivery && !r.delivery.sent ? ` (${r.delivery.error ?? "WhatsApp notification not sent; the outcome is in the conversation"})` : "";
      onDecided(`Approval #${approval.id} has been ${decision === "approve" ? "approved and executed" : "rejected"}, and the customer has been sent: ${text}${notice}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const kb = approval.evidence.filter((e) => e.kind === "kb");
  const tools = approval.evidence.filter((e) => e.kind === "tool");
  return (
    <div className="card">
      <div className="card-head">
        <div className="row">
          <b>#{approval.id} {TOOL_TEXT[approval.tool_name] ?? approval.tool_name}</b>
          <StatusBadge status={approval.status} />
        </div>
        <span className="faint">{time(approval.created_at)}</span>
      </div>
      <div className="card-pad grid grid-2">
        <div className="stack">
          <div className="faint">Requested by</div>
          <div>{approval.requested_by?.name} ({ROLE_TEXT[approval.requested_by?.role ?? ""]})</div>
          <div className="faint">Original request</div>
          <div>{approval.question}</div>
          <div className="faint">Action parameters</div>
          <div className="row">
            <span className="mono">{String(approval.arguments.order_id ?? "")}</span>
            <b style={{ fontSize: 20 }}>{aud(approval.arguments.amount)}</b>
          </div>
          <div className="badge badge-warning" style={{ width: "fit-content" }}>{approval.reason}</div>
          {approval.required_role === "admin" && (
            <div className="notice">The amount is above the tenant's approval threshold, so an admin must approve it.</div>
          )}
        </div>
        <div className="stack">
          <div className="faint">Evidence pack submitted by the agent ({approval.evidence.length} items)</div>
          {kb.map((e) => (
            <div key={e.ref} className="json small">
              <b>[{e.ref}] {e.title}</b> <span className="faint">{e.meta.heading}</span>
              <div style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{e.text}</div>
            </div>
          ))}
          {tools.map((e) => (
            <details key={e.ref} className="small">
              <summary style={{ cursor: "pointer" }}>[{e.ref}] Tool result: {TOOL_TEXT[e.title] ?? e.title}</summary>
              <pre className="json mono">{(() => { try { return JSON.stringify(JSON.parse(e.text), null, 2); } catch { return e.text; } })()}</pre>
            </details>
          ))}
        </div>
      </div>
      {approval.status === "pending" ? (
        <div className="card-pad stack" style={{ borderTop: "1px solid var(--border)" }}>
          <ErrorBanner error={error} />
          {own && <div className="notice">You raised this request yourself, so under the four-eyes principle another colleague needs to approve it.</div>}
          {needsAdmin && !own && <div className="notice">This amount needs an admin to approve it; you can reject it (with a reason), or leave it for an admin.</div>}
          <div className="row decision-row">
            <input className="input" placeholder="Approval comment (the customer is told); required when rejecting" value={comment} onChange={(e) => setComment(e.target.value)} />
            <button className="btn btn-danger" disabled={busy || own || !comment.trim()} title={comment.trim() ? undefined : "A rejection needs a reason"}
              onClick={() => decide("reject")}><X size={15} /> Reject</button>
            <button className="btn btn-success" disabled={busy || own || needsAdmin} onClick={() => decide("approve")}><Check size={15} /> Approve and execute</button>
          </div>
          {result && <div className="notice">Customer notified: {result}</div>}
        </div>
      ) : (
        <div className="card-pad faint" style={{ borderTop: "1px solid var(--border)" }}>
          {approval.reviewer?.name} handled this on {time(approval.decided_at)}{approval.comment && `, comment: ${approval.comment}`} ·{" "}
          <Link to={`/traces/${approval.run_id}`}>View trace</Link>
        </div>
      )}
    </div>
  );
}

export default function ApprovalsPage() {
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const { data, error, loading, reload } = useLoad(() => api.get<Approval[]>(`/api/approvals?status_filter=${tab}`), [tab]);
  const [lastDecision, setLastDecision] = useState<string | null>(null);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Approval queue</h1>
          <p>Refunds above the tenant's auto-approval limit pause here. Once approved, the agent re-checks the order status before executing it and replies to the customer with the outcome.</p>
        </div>
        <div className="tabs">
          <button className={tab === "pending" ? "active" : ""} onClick={() => setTab("pending")}>Pending</button>
          <button className={tab === "all" ? "active" : ""} onClick={() => setTab("all")}>All</button>
        </div>
      </div>
      <ErrorBanner error={error} />
      {lastDecision && <div className="notice" style={{ marginBottom: 12 }}>{lastDecision}</div>}
      {loading && !data ? <Empty>Loading…</Empty> : data?.length === 0 ? (
        <Empty>No approvals right now. Sign in with a customer account and ask for a refund on order SO20261008002 in the chat to create one.</Empty>
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          {data?.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={(summary) => { setLastDecision(summary); reload(); }} />)}
        </div>
      )}
    </div>
  );
}
