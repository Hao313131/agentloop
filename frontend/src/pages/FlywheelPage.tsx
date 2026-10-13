import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, X } from "lucide-react";
import { ApiError, api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, Kpi, StatusBadge, pct, time, useLoad } from "../components/ui";

interface FeedbackRow {
  id: number;
  rating: string;
  question: string;
  answer: string;
  correction: string;
  reason_category: string;
  attribution: string;
  run_id: string | null;
  user: { name: string; role: string };
  created_at: string;
  patch: { id: number; status: string } | null;
  eval_case: { id: number; key: string; status: string } | null;
}

interface Patch {
  id: number;
  feedback_id: number;
  title: string;
  content: string;
  status: string;
  created_at: string;
}

interface PatchConflict {
  patch_sentence: string;
  document: string;
  existing_sentence: string;
}

interface Stats {
  feedback: Record<string, number>;
  attribution: Record<string, number>;
  reason_labels: Record<string, string>;
  patches: Record<string, number>;
  eval_cases: { total: number; from_flywheel: number; candidates: number };
  pass_rate_history: { id: number; pass_rate: number; total: number }[];
}

const RATING: Record<string, [string, string]> = {
  up: ["Helpful", "badge-success"],
  down: ["Not helpful", "badge-danger"],
  correction: ["Correction", "badge-warning"],
};

function Sparkline({ values }: { values: { pass_rate: number; total: number }[] }) {
  if (values.length < 2) return <span className="faint">The trend appears after two eval runs</span>;
  const w = 260, h = 56;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - v.pass_rate * (h - 8) - 4}`).join(" ");
  return (
    <svg width={w} height={h} role="img" aria-label="Eval pass rate over time">
      <polyline points={points} fill="none" stroke="var(--primary)" strokeWidth={2} />
      {values.map((v, i) => (
        <circle key={i} cx={(i / (values.length - 1)) * w} cy={h - v.pass_rate * (h - 8) - 4} r={3} fill="var(--primary)">
          <title>{`${pct(v.pass_rate)} (${v.total} cases)`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function FlywheelPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const stats = useLoad(() => api.get<Stats>("/api/flywheel/stats"));
  const feedback = useLoad(() => api.get<FeedbackRow[]>("/api/flywheel/feedback"));
  const patches = useLoad(() => api.get<Patch[]>("/api/flywheel/patches"));
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A patch that contradicts the knowledge the corrected answer cited waits for an explicit decision.
  const [disputed, setDisputed] = useState<{ patch: Patch; reason: string; conflicts: PatchConflict[] } | null>(null);

  async function act(patch: Patch, action: "apply" | "reject", acknowledge = false) {
    try {
      const r = await api.post<{ document_version?: number; conflicts?: number; eval_case_activated?: boolean }>(
        `/api/flywheel/patches/${patch.id}/${action}`, action === "apply" ? { acknowledge_conflicts: acknowledge } : {},
      );
      setDisputed(null);
      setMessage(action === "reject"
        ? `Patch #${patch.id} was rejected`
        : r.eval_case_activated === false
          ? `Patch #${patch.id} is published (version v${r.document_version}). Because it contradicts the original document, its eval case stays a candidate: change the original document first, then activate it in the eval centre.`
          : `Patch #${patch.id} is published to the knowledge base (version v${r.document_version}) and its eval case is active.` +
            (r.conflicts ? ` Note: the patch contradicts existing knowledge in ${r.conflicts} place(s) — check it under “Knowledge base”.` : " Run the regression eval to confirm it went from failing to passing."));
      setError(null);
      patches.reload();
      stats.reload();
      feedback.reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && Array.isArray(e.body?.conflicts)) {
        setDisputed({ patch, reason: e.message, conflicts: e.body.conflicts as PatchConflict[] });
        setError(null);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const s = stats.data;
  const labels = s?.reason_labels ?? {};
  const attributionTotal = Object.values(s?.attribution ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Data flywheel</h1>
          <p>One correction from a support agent → a failure attribution, a regression eval case and a draft knowledge patch. Once an admin publishes the patch, the knowledge base is re-indexed, the case becomes active, and the regression eval closes the loop.</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      {disputed && (
        <div className="card" role="alert">
          <div className="card-head"><h3>Patch #{disputed.patch.id} contradicts existing knowledge</h3></div>
          <div className="card-pad stack">
            <div className="small">{disputed.reason}</div>
            <div className="small faint">If you publish it anyway, these conflicts go on the knowledge base's review list; until they are checked, a customer question that touches both versions goes to a person.</div>
            {disputed.conflicts.map((c, i) => (
              <div key={i} className="json small stack" style={{ gap: 4 }}>
                <div className="quote"><b>Patch</b> {c.patch_sentence}</div>
                <div className="quote"><b>{c.document}</b> {c.existing_sentence}</div>
              </div>
            ))}
            <div className="row">
              <button className="btn btn-sm btn-danger" onClick={() => act(disputed.patch, "reject")}><X size={14} /> Reject the patch</button>
              <button className="btn btn-sm" onClick={() => act(disputed.patch, "apply", true)}>I have fixed the original document — publish anyway (the case stays a candidate)</button>
              <button className="btn btn-sm btn-text" onClick={() => setDisputed(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
      {message && <div className="notice">{message} <Link to="/evals">Go to the eval centre →</Link></div>}
      <div className="grid grid-4">
        <Kpi label="Feedback received" value={(s?.feedback.up ?? 0) + (s?.feedback.down ?? 0) + (s?.feedback.correction ?? 0)}
          sub={`Helpful ${s?.feedback.up ?? 0} · Not helpful ${s?.feedback.down ?? 0} · Corrections ${s?.feedback.correction ?? 0}`} />
        <Kpi label="Knowledge patches" value={s?.patches.applied ?? 0} sub={`Published · Awaiting review ${s?.patches.proposed ?? 0} · Rejected ${s?.patches.rejected ?? 0}`} />
        <Kpi label="Eval cases" value={s?.eval_cases.total ?? 0} sub={`Of which from the flywheel ${s?.eval_cases.from_flywheel ?? 0} · Candidates ${s?.eval_cases.candidates ?? 0}`} />
        <div className="card kpi">
          <div className="kpi-label">Regression pass rate</div>
          <Sparkline values={s?.pass_rate_history ?? []} />
        </div>
      </div>
      <div className="grid grid-2">
        <div className="card">
          <div className="card-head"><h3>Failure attribution</h3><span className="faint">Derived from the trace automatically; a person can override it</span></div>
          <div className="card-pad stack">
            {attributionTotal === 0 && <Empty>No negative feedback or corrections yet</Empty>}
            {Object.entries(s?.attribution ?? {}).map(([k, v]) => (
              <div key={k}>
                <div className="row small"><span>{labels[k] ?? k}</span><span className="spacer" /><span className="mono">{v}</span></div>
                <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / attributionTotal) * 100}%` }} /></div>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h3>Knowledge patches</h3><span className="faint">{admin ? "Published, once reviewed, to the “Corrections from support” document" : "Only an admin can publish"}</span></div>
          <div className="card-pad stack">
            {patches.data?.length === 0 && <Empty>Use the “correct this answer” button on a knowledge answer in a conversation and a draft patch appears here.</Empty>}
            {patches.data?.map((p) => (
              <div key={p.id} className="json">
                <div className="row"><b>#{p.id}</b><StatusBadge status={p.status} /><span className="spacer" /><span className="faint">{time(p.created_at)}</span></div>
                <pre style={{ fontFamily: "inherit", margin: "6px 0" }}>{p.content}</pre>
                {admin && p.status === "proposed" && (
                  <div className="row">
                    <button className="btn btn-sm btn-success" onClick={() => act(p, "apply")}><Check size={14} /> Publish</button>
                    <button className="btn btn-sm btn-danger" onClick={() => act(p, "reject")}><X size={14} /> Reject</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="card table-wrap">
        <div className="card-head"><h3>Feedback log</h3></div>
        <table className="table">
          <thead><tr><th>Time</th><th>Type</th><th>Question</th><th>Answer → correction</th><th>Attribution</th><th>Produced</th></tr></thead>
          <tbody>
            {feedback.data?.map((f) => (
              <tr key={f.id}>
                <td className="faint nowrap">{time(f.created_at)}<div>{f.user.name}</div></td>
                <td><span className={`badge ${RATING[f.rating]?.[1] ?? ""}`}>{RATING[f.rating]?.[0] ?? f.rating}</span></td>
                <td style={{ maxWidth: 200 }}>{f.question}</td>
                <td style={{ maxWidth: 380 }}>
                  <div className="small muted">{f.answer}</div>
                  {f.correction && <div className="small" style={{ color: "var(--success)" }}>→ {f.correction}</div>}
                </td>
                <td>{f.reason_category ? <span className="badge">{labels[f.reason_category] ?? f.reason_category}</span> : "—"}</td>
                <td className="stack small">
                  {f.eval_case && <span>Case {f.eval_case.key} <StatusBadge status={f.eval_case.status} /></span>}
                  {f.patch && <span>Patch #{f.patch.id} <StatusBadge status={f.patch.status} /></span>}
                  {f.run_id && <Link to={`/traces/${f.run_id}`}>Trace</Link>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {feedback.data?.length === 0 && <Empty>No feedback yet</Empty>}
      </div>
    </div>
  );
}
