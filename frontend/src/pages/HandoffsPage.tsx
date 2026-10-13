import { useCallback, useEffect, useState } from "react";
import { Send, Undo2 } from "lucide-react";
import { api } from "../api";
import { CHANNEL_TEXT, Empty, ErrorBanner, time, useLoad } from "../components/ui";
import type { ChatMessage, Handoff } from "../types";

function waited(seconds: number) {
  if (seconds < 60) return "under a minute";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

function HandoffPanel({ handoff, onChanged }: { handoff: Handoff; onChanged: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await api.get<{ messages: ChatMessage[] }>(`/api/conversations/${handoff.conversation_id}/messages`);
    setMessages(r.messages);
  }, [handoff.conversation_id]);

  // The customer can keep writing while waiting; refresh so new messages show up.
  useEffect(() => {
    load().catch((e) => setError(e.message));
    const timer = window.setInterval(() => load().catch(() => undefined), 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function act(path: "reply" | "close") {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/handoffs/${handoff.ticket_id}/${path}`, path === "reply" ? { content: text } : {});
      if (path === "reply") {
        setText("");
        await load();
      }
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const speaker = (m: ChatMessage) =>
    m.role === "user" ? handoff.customer : m.role === "agent" ? `Support agent ${m.meta.agent ?? ""}` : m.role === "system" ? "System" : "Assistant";
  return (
    <div className="card handoff-panel">
      <div className="card-head">
        <div className="row">
          <b>{handoff.customer}</b>
          <span className="badge">{CHANNEL_TEXT[handoff.channel ?? ""] ?? handoff.channel}</span>
          {handoff.assignee ? <span className="badge badge-info">{handoff.assignee} is on it</span> : <span className="badge badge-warning">Waiting to be picked up</span>}
        </div>
        <button className="btn btn-sm" disabled={busy} onClick={() => act("close")}>
          <Undo2 size={14} /> End human service
        </button>
      </div>
      <div className="card-pad handoff-transcript" aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`transcript-line ${m.role}`}>
            <div className="faint small">{speaker(m)} · {time(m.created_at)}</div>
            <div className="bubble-text">{m.content}</div>
          </div>
        ))}
      </div>
      <div className="card-pad stack" style={{ borderTop: "1px solid var(--border)" }}>
        <ErrorBanner error={error} />
        <div className="row decision-row">
          <textarea className="textarea" rows={2} maxLength={2000} placeholder="Reply to the customer as a support agent" value={text}
            onChange={(e) => setText(e.target.value)} />
          <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => act("reply")}>
            <Send size={15} /> Send reply
          </button>
        </div>
        <div className="faint small">Whoever replies first owns the follow-up. Ending human service hands the conversation back to the assistant, and the customer is told.</div>
      </div>
    </div>
  );
}

export default function HandoffsPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<Handoff[]>("/api/handoffs"), []);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const current = data?.find((h) => h.ticket_id === selected) ?? data?.[0] ?? null;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Human support</h1>
          <p>Once a customer is handed to a person the assistant stops answering that conversation, and anything they type appears here. Take the longest wait first; when you are done, end human service and the conversation goes back to the assistant.</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      {loading && !data ? <Empty>Loading…</Empty> : !data?.length ? (
        <Empty>No customers are waiting for a person. Sign in as a customer and type “transfer me to a human” in the chat to create one.</Empty>
      ) : (
        <div className="handoff-layout">
          <div className="stack handoff-list" role="list" aria-label="Conversations waiting for a person">
            {data.map((h) => (
              <button key={h.ticket_id} role="listitem" className={`card handoff-item ${h === current ? "active" : ""}`}
                aria-current={h === current} onClick={() => setSelected(h.ticket_id)}>
                <div className="row">
                  <b>{h.customer}</b>
                  <span className="spacer" />
                  {h.position ? <span className="badge badge-warning">Queue position {h.position}</span> : <span className="badge badge-info">{h.assignee}</span>}
                </div>
                <div className="faint small ellipsis">{h.last_message || h.reason}</div>
                <div className="faint small">Waiting {waited(h.waiting_seconds)}</div>
              </button>
            ))}
          </div>
          {current && <HandoffPanel key={current.ticket_id} handoff={current} onChanged={() => void reload()} />}
        </div>
      )}
    </div>
  );
}
