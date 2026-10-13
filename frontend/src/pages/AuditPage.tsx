import { useState } from "react";
import { api } from "../api";
import { Empty, ErrorBanner, time, useLoad } from "../components/ui";

interface AuditEntry {
  id: number;
  action: string;
  action_label: string;
  target: string;
  detail: Record<string, unknown>;
  request_id: string;
  created_at: string;
  actor: { id: number; name: string; email: string } | null;
}

const LABEL: Record<string, string> = {
  auto_refund_limit: "Automatic refund limit",
  ticket_sla_hours: "Ticket response time (hours)",
  return_window_days: "Change of mind window (days)",
  quality_window_days: "Faulty item window (days)",
  title: "Title",
  visibility: "Visibility",
  status: "Status",
  question: "Question",
  must_include: "Must include",
  must_not_include: "Must not include",
  expected_tools: "Expected tools",
  version: "Version",
  from_version: "Previous version",
  to_version: "New version",
  file: "File",
  content: "Content",
  document_version: "Patch document version",
  acknowledged_conflicts: "Conflicts acknowledged",
  key: "Case",
  sentence: "Sentence",
  other_sentence: "Other sentence",
  note: "Note",
  erased: "Records erased",
  approval_admin_threshold: "Admin approval threshold (AUD)",
  timezone: "Time zone",
  tool: "Tool",
  order_id: "Order",
  amount: "Amount (AUD)",
  comment: "Comment",
  decision: "Original decision",
  error: "Error type",
  days: "Days",
  rows: "Rows",
  conversation_id: "Conversation",
  message_id: "Message",
  overdue: "Past the promised time",
};

const VALUE: Record<string, string> = {
  public: "Public", internal: "Internal", active: "Active", candidate: "Candidate", archived: "Archived",
  approve: "Approved", reject: "Rejected", request_refund: "Refund",
};

const CATEGORIES: [string, string][] = [
  ["", "All categories"], ["tenant", "Tenant settings"], ["kb", "Knowledge base"], ["flywheel", "Knowledge patches"], ["eval", "Eval cases"],
  ["user", "Personal data"], ["approval", "Approvals"], ["export", "Data exports"], ["handoff", "Human support"], ["ticket", "Tickets"],
];

function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string" && VALUE[value]) return VALUE[value];
  if (Array.isArray(value)) return value.map(show).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${k} ${show(v)}`).join("; ");
  return String(value);
}

function Detail({ detail }: { detail: Record<string, unknown> }) {
  const { changes, diff, ...rest } = detail as { changes?: Record<string, [unknown, unknown]>; diff?: string };
  return (
    <div className="stack" style={{ gap: 4 }}>
      {changes && Object.entries(changes).map(([field, [before, after]]) => (
        <div key={field}>{LABEL[field] ?? field}: <span className="faint">{show(before)}</span> → <b>{show(after)}</b></div>
      ))}
      {Object.entries(rest).map(([key, value]) => (
        <div key={key} className="small"><span className="faint">{LABEL[key] ?? key}: </span>{show(value)}</div>
      ))}
      {diff && (
        <details>
          <summary className="small">View the changed lines</summary>
          <pre className="json small" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{diff}</pre>
        </details>
      )}
    </div>
  );
}

interface Integrity {
  ok: boolean;
  entries: number;
  last_seq: number;
  altered: number[];
  missing: number[];
  unsigned: number;
}

function IntegrityNotice() {
  const { data, error } = useLoad(() => api.get<Integrity>("/api/audit/verify"));
  if (error) return <div className="error-banner" role="alert">The integrity check could not run: {error}</div>;
  if (!data) return null;
  if (data.ok) {
    return (
      <div className="notice" role="status">
        Integrity check passed: {data.last_seq} {data.last_seq === 1 ? "entry" : "entries"}, consecutively numbered and correctly signed{data.unsigned ? ` (${data.unsigned} more predate signing)` : ""}.
      </div>
    );
  }
  return (
    <div className="error-banner" role="alert">
      Integrity check failed: {data.altered.length ? `${data.altered.length} entries no longer match their signature` : ""}
      {data.altered.length && data.missing.length ? ", " : ""}
      {data.missing.length ? `missing sequence numbers ${data.missing.join(", ")}` : ""}
      {(data.altered.length || data.missing.length) && data.unsigned ? ", " : ""}
      {data.unsigned ? `${data.unsigned} entries are unsigned (older records can be signed once by operations; otherwise someone wrote to the audit table directly)` : ""}.
      The audit log may have been changed directly in the database.
    </div>
  );
}

export default function AuditPage() {
  const [category, setCategory] = useState("");
  const { data, error } = useLoad(
    () => api.get<AuditEntry[]>(`/api/audit?limit=200${category ? `&category=${category}` : ""}`), [category]);
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Operation audit</h1>
          <p>An admin's changes to risk limits, knowledge documents, knowledge patches, eval cases and personal data, plus staff approval decisions, data exports, human replies and ticket handling: who did what, and when. What the agent did itself is on the traces page.</p>
        </div>
        <div className="row">
          <select className="select" style={{ width: 170 }} value={category} onChange={(e) => setCategory(e.target.value)}
            aria-label="Audit category">
            {CATEGORIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
      </div>
      <ErrorBanner error={error} />
      <IntegrityNotice />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Object</th><th>Detail</th></tr></thead>
          <tbody>
            {data?.map((e) => (
              <tr key={e.id}>
                <td className="faint nowrap" title={`Request ID: ${e.request_id}`}>{time(e.created_at)}</td>
                <td>{e.actor ? <>{e.actor.name}<div className="faint small">{e.actor.email}</div></> : <span className="faint">—</span>}</td>
                <td className="nowrap">{e.action_label}</td>
                <td className="faint">{e.target}</td>
                <td><Detail detail={e.detail} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <Empty>{category ? "Nothing recorded in this category yet" : "Nothing recorded yet"}</Empty>}
      </div>
    </div>
  );
}
