import { useRef, useState } from "react";
import { AlertTriangle, Check, FilePlus2, Pencil, Search, Trash2 } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Drawer, Empty, ErrorBanner, Modal, pressable, time, useLoad } from "../components/ui";
import type { DocumentRow } from "../types";

interface DocDetail extends DocumentRow {
  content: string;
  chunks: { id: number; ord: number; heading: string; text: string; page: number | null; flagged_injection: boolean }[];
}

interface SearchHit {
  chunk_id: number;
  doc_title: string;
  heading: string;
  text: string;
  rrf: number;
  relevance: number;
  coverage: number;
  cosine: number;
  bm25_rank: number | null;
  vector_rank: number | null;
  flagged_injection: boolean;
  passes_threshold: boolean;
}

function RetrievalDebugger() {
  const [query, setQuery] = useState("how long does a refund take");
  const [scope, setScope] = useState("customer");
  const [result, setResult] = useState<{ query_tokens: string[]; coverage_tokens: string[]; min_relevance: number; hits: SearchHit[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    try {
      setResult(await api.post("/api/kb/search", { query, scope, top_k: 6 }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <div className="card">
      <div className="card-head">
        <h3>Retrieval debugger</h3>
        <span className="faint">BM25 + vector → RRF fusion → relevance threshold</span>
      </div>
      <div className="card-pad stack">
        <div className="row">
          <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
          <select className="select" style={{ width: 160 }} value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="customer">Customer-visible</option>
            <option value="staff">Staff-visible</option>
          </select>
          <button className="btn btn-primary" onClick={run}><Search size={15} /> Search</button>
        </div>
        <ErrorBanner error={error} />
        {result && (
          <>
            <div className="faint">
              Recall tokens: {result.query_tokens.join(" / ") || "(none)"} · Coverage tokens (spoken wording mapped to the knowledge base's wording):{" "}
              {result.coverage_tokens.join(" / ") || "(none)"} · Relevance threshold {result.min_relevance}
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>Passage</th><th>BM25 rank</th><th>Vector rank</th><th>RRF</th><th>Term coverage</th><th>Cosine</th><th>Relevance</th></tr>
                </thead>
                <tbody>
                  {result.hits.map((h) => (
                    <tr key={h.chunk_id} style={{ opacity: h.passes_threshold && !h.flagged_injection ? 1 : 0.5 }}>
                      <td style={{ maxWidth: 360 }}>
                        <div><b>{h.doc_title}</b> <span className="faint">{h.heading}</span></div>
                        <div className="small muted">{h.text.slice(0, 90)}</div>
                        {h.flagged_injection && <span className="badge badge-danger"><AlertTriangle size={12} /> Injection sample, blocked</span>}
                        {!h.passes_threshold && <span className="badge">Below the threshold, not put in context</span>}
                      </td>
                      <td>{h.bm25_rank ?? "—"}</td>
                      <td>{h.vector_rank ?? "—"}</td>
                      <td className="mono">{h.rrf.toFixed(4)}</td>
                      <td className="mono">{h.coverage.toFixed(2)}</td>
                      <td className="mono">{h.cosine.toFixed(2)}</td>
                      <td><b className="mono">{h.relevance.toFixed(2)}</b></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {result.hits.length === 0 && <Empty>No passage matched</Empty>}
          </>
        )}
      </div>
    </div>
  );
}

function AddDocModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [mode, setMode] = useState<"text" | "file">("text");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [visibility, setVisibility] = useState("public");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      if (mode === "text") await api.post("/api/kb/documents/text", { title, content, visibility });
      else {
        const f = file.current?.files?.[0];
        if (!f) throw new Error("Choose a file first");
        const form = new FormData();
        form.append("file", f);
        form.append("visibility", visibility);
        if (title) form.append("title", title);
        await api.upload("/api/kb/documents/upload", form);
      }
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title="Add a knowledge document" onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={busy} onClick={submit}>Ingest and index</button></>}>
      <div className="tabs" style={{ width: "fit-content" }}>
        <button className={mode === "text" ? "active" : ""} onClick={() => setMode("text")}>Paste Markdown</button>
        <button className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}>Upload a file</button>
      </div>
      <label className="field">Title<input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={mode === "file" ? "Optional; the file name is used by default" : ""} /></label>
      {mode === "text" ? (
        <label className="field">Content (split into sections at each ## heading)
          <textarea className="textarea" style={{ minHeight: 180 }} value={content} onChange={(e) => setContent(e.target.value)}
            placeholder={"## Trade-ins\n- Trade-ins are limited to phones and tablets."} />
        </label>
      ) : (
        <label className="field">File (.md / .txt / .pdf, up to 5MB)<input ref={file} type="file" accept=".md,.markdown,.txt,.pdf" /></label>
      )}
      <label className="field">Who can see it
        <select className="select" value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <option value="public">Public: customers and staff can both retrieve it</option>
          <option value="internal">Internal: staff only</option>
        </select>
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

function EditDocModal({ doc, onClose, onDone }: { doc: DocDetail; onClose: () => void; onDone: () => void }) {
  const [title, setTitle] = useState(doc.title);
  const [content, setContent] = useState(doc.content);
  const [visibility, setVisibility] = useState(doc.visibility);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      // The version this edit was made on: if someone saved in between, the server refuses
      // instead of overwriting their change.
      await api.put(`/api/kb/documents/${doc.id}`, { title, content, visibility, version: doc.version });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title={`Edit “${doc.title}”`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={busy || !title.trim() || !content.trim()} onClick={submit}>Save as v{doc.version + 1}</button></>}>
      <label className="field">Title<input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="field">Content (saving re-splits, re-indexes and re-checks for knowledge conflicts)
        <textarea className="textarea" style={{ minHeight: 280 }} value={content} onChange={(e) => setContent(e.target.value)} />
      </label>
      <label className="field">Who can see it
        <select className="select" value={visibility} onChange={(e) => setVisibility(e.target.value as DocDetail["visibility"])}>
          <option value="public">Public: customers and staff can both retrieve it</option>
          <option value="internal">Internal: staff only</option>
        </select>
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

interface Conflict {
  id: number;
  sentence: string;
  other_sentence: string;
  document: { id: number; title: string };
  other_document: { id: number; title: string };
  detail: Record<string, { this: string[]; other: string[] }>;
  origin: "detected" | "flywheel";
  note: string;
  created_at: string;
}

function ConflictsCard({ admin, onChange }: { admin: boolean; onChange: () => void }) {
  const { data, reload } = useLoad(() => api.get<Conflict[]>("/api/kb/conflicts"));
  if (!data || data.length === 0) return null;
  async function resolve(id: number) {
    try {
      await api.post(`/api/kb/conflicts/${id}/resolve`, { note: "Checked" });
    } catch {
      // 409: another administrator resolved it, or its document was deleted; the reload shows that
    } finally {
      reload();
      onChange();
    }
  }
  return (
    <div className="card">
      <div className="card-head">
        <h3>Knowledge conflicts ({data.length})</h3>
        <span className="faint">The same rule is stated with different numbers in two documents. Until it is checked, a customer question that touches both versions goes to a person</span>
      </div>
      <div className="card-pad stack">
        {data.map((c) => (
          <div key={c.id} className="json small stack" style={{ gap: 6 }}>
            <div className="row">
              {Object.entries(c.detail).map(([unit, v]) => (
                <span key={unit} className="badge badge-warning">{unit}: {v.this.join(", ")} ≠ {v.other.join(", ")}</span>
              ))}
              {c.origin === "flywheel" && <span className="badge" title={c.note}>From a correction patch that was published</span>}
              <span className="spacer" />
              <span className="faint">{time(c.created_at)}</span>
            </div>
            <div className="quote"><b>{c.document.title}</b> {c.sentence}</div>
            <div className="quote"><b>{c.other_document.title}</b> {c.other_sentence}</div>
            {admin && (
              <button className="btn btn-sm" style={{ width: "fit-content" }} onClick={() => resolve(c.id)}>
                <Check size={14} /> Checked — mark as resolved
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function KnowledgePage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { data, error, reload } = useLoad(() => api.get<DocumentRow[]>("/api/kb/documents"));
  const [detail, setDetail] = useState<DocDetail | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [revision, setRevision] = useState(0); // remounts the conflicts card after a document changes
  function changed() {
    reload();
    setRevision((r) => r + 1);
  }

  async function open(id: number) {
    setDetail(await api.get<DocDetail>(`/api/kb/documents/${id}`));
  }
  async function remove(id: number) {
    if (!window.confirm("Deleting this document removes every passage of it from the index. Continue?")) return;
    await api.del(`/api/kb/documents/${id}`);
    setDetail(null);
    changed();
  }

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Knowledge base</h1>
          <p>Documents are split by heading and keep their section path. Ingesting scans for prompt injection, and a passage that matches never reaches the model's context.</p>
        </div>
        {admin && <button className="btn btn-primary" onClick={() => setAdding(true)}><FilePlus2 size={16} /> Add document</button>}
      </div>
      <ErrorBanner error={error} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>Title</th><th>Visibility</th><th>Passages</th><th>Injection scan</th><th>Conflicts</th><th>Version</th><th>Updated</th></tr></thead>
          <tbody>
            {data?.map((d) => (
              <tr key={d.id} className="clickable" {...pressable(() => open(d.id))}>
                <td><b>{d.title}</b> <span className="faint">{d.source}</span></td>
                <td>{d.visibility === "public" ? <span className="badge badge-primary">Public</span> : <span className="badge badge-info">Internal</span>}</td>
                <td>{d.chunk_count}</td>
                <td>{d.flagged_chunks ? <span className="badge badge-danger"><AlertTriangle size={12} /> {d.flagged_chunks} blocked</span> : <span className="faint">None</span>}</td>
                <td>{d.conflicts ? <span className="badge badge-warning">{d.conflicts} to check</span> : <span className="faint">None</span>}</td>
                <td>v{d.version}</td>
                <td className="faint">{time(d.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ConflictsCard key={revision} admin={admin} onChange={reload} />
      <RetrievalDebugger />
      {adding && <AddDocModal onClose={() => setAdding(false)} onDone={() => { setAdding(false); changed(); }} />}
      {editing && detail && <EditDocModal doc={detail} onClose={() => setEditing(false)}
        onDone={() => { setEditing(false); changed(); open(detail.id); }} />}
      {detail && (
        <Drawer title={<div><h2>{detail.title}</h2><div className="faint">{detail.chunk_count} passages · v{detail.version}</div></div>} onClose={() => setDetail(null)}>
          <div className="stack">
            {admin && (
              <div className="row">
                {detail.chunks.some((c) => c.page) ? <span className="faint small">For a PDF, upload a new version of the file</span>
                  : <button className="btn" onClick={() => setEditing(true)}><Pencil size={15} /> Edit</button>}
                <button className="btn btn-danger" onClick={() => remove(detail.id)}><Trash2 size={15} /> Delete document</button>
              </div>
            )}
            {detail.chunks.map((c) => (
              <div key={c.id} className="json" style={c.flagged_injection ? { outline: "2px solid var(--danger)" } : undefined}>
                <div className="row faint">#{c.ord} · {c.heading || "(no heading)"}{c.page && ` · page ${c.page}`}
                  {c.flagged_injection && <span className="badge badge-danger">Possible prompt injection, blocked</span>}
                </div>
                <div style={{ whiteSpace: "pre-wrap" }}>{c.text}</div>
              </div>
            ))}
          </div>
        </Drawer>
      )}
    </div>
  );
}
