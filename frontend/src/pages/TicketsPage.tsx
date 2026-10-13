import { useState } from "react";
import { Check, Hand } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Modal, time, useLoad } from "../components/ui";

interface Ticket {
  id: number;
  title: string;
  description: string;
  priority: "low" | "normal" | "high" | "urgent";
  status: "open" | "in_progress" | "closed";
  conversation_id: number | null;
  resolution: string;
  customer: { id: number; name: string } | null;
  assignee: { id: number; name: string } | null;
  created_at: string;
  due_at: string;
  closed_at: string | null;
  overdue: boolean;
}

const PRIORITY: Record<Ticket["priority"], string> = { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" };
const STATUS: Record<Ticket["status"], string> = { open: "Open", in_progress: "In progress", closed: "Done" };

function remaining(ticket: Ticket) {
  if (ticket.status === "closed") return <span className="faint">{time(ticket.closed_at)} · done</span>;
  const minutes = Math.round((new Date(ticket.due_at).getTime() - Date.now()) / 60000);
  const span = Math.abs(minutes) < 60 ? `${Math.abs(minutes)} min` : `${Math.floor(Math.abs(minutes) / 60)} h ${Math.abs(minutes) % 60} min`;
  return ticket.overdue
    ? <span className="badge badge-danger">Overdue by {span}</span>
    : <span className={minutes < 60 ? "badge badge-warning" : "faint"}>{span} left</span>;
}

function CloseModal({ ticket, onClose, onDone }: { ticket: Ticket; onClose: () => void; onDone: () => void }) {
  const [resolution, setResolution] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/tickets/${ticket.id}/close`, { resolution });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title={`Handle ticket #${ticket.id}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={busy || !resolution.trim()} onClick={submit}>Done — let the customer know</button></>}>
      <div className="small faint">{ticket.title}</div>
      <label className="field">Outcome (sent to the customer in the conversation)
        <textarea className="textarea" style={{ minHeight: 120 }} value={resolution} onChange={(e) => setResolution(e.target.value)}
          placeholder="e.g. A technician is booked to visit tomorrow morning" />
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

export default function TicketsPage() {
  const [showClosed, setShowClosed] = useState(false);
  const { data, error, reload } = useLoad(() => api.get<Ticket[]>(`/api/tickets${showClosed ? "?status_filter=all" : ""}`), [showClosed]);
  const [closing, setClosing] = useState<Ticket | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function claim(ticket: Ticket) {
    setActionError(null);
    try {
      await api.post(`/api/tickets/${ticket.id}/claim`);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
    reload();
  }

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Tickets</h1>
          <p>Tickets the agent has created for customers, sorted by the response time promised to them. Claim one to work on it; the outcome is sent to the customer's conversation.</p>
        </div>
        <label className="row small"><input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> Show done</label>
      </div>
      <ErrorBanner error={error ?? actionError} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>Ticket</th><th>Customer</th><th>Priority</th><th>Status</th><th>Promised by</th><th>Claimed by</th><th /></tr></thead>
          <tbody>
            {data?.map((t) => (
              <tr key={t.id}>
                <td><b>#{t.id} {t.title}</b>
                  {/* the agent often files the customer's own sentence as both title and description */}
                  {t.description && t.description.trim() !== t.title.trim() && <div className="faint small">{t.description}</div>}
                  {t.resolution && <div className="small">Outcome: {t.resolution}</div>}</td>
                <td className="nowrap">{t.customer?.name ?? "—"}</td>
                <td className="nowrap">{t.priority === "high" || t.priority === "urgent" ? <span className="badge badge-warning">{PRIORITY[t.priority]}</span> : PRIORITY[t.priority]}</td>
                <td className="nowrap">{STATUS[t.status]}</td>
                <td className="nowrap">{remaining(t)}</td>
                <td className="nowrap">{t.assignee?.name ?? <span className="faint">—</span>}</td>
                <td>
                  <div className="row">
                    {t.status === "open" && <button className="btn btn-sm" onClick={() => claim(t)}><Hand size={14} /> Claim</button>}
                    {t.status !== "closed" && <button className="btn btn-sm btn-primary" onClick={() => setClosing(t)}><Check size={14} /> Mark as done</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <Empty>{showClosed ? "No tickets yet" : "No open tickets to work on"}</Empty>}
      </div>
      {closing && <CloseModal ticket={closing} onClose={() => setClosing(null)} onDone={() => { setClosing(null); reload(); }} />}
    </div>
  );
}
