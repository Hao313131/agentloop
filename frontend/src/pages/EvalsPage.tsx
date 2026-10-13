import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, Kpi, StatusBadge, ms, pct, time, useLoad } from "../components/ui";
import type { EvalCase, EvalRunRow } from "../types";

const CHECK_TEXT: Record<string, string> = {
  tools: "Tool calls",
  forbidden_tools: "Forbidden tools",
  must_include: "Must include",
  must_not_include: "Must not include",
  refusal: "Refusal",
  approval: "Approval triggered",
  retrieval_hit: "Retrieval hit",
  evidence: "Evidence check",
};

interface RunDetail extends EvalRunRow {
  results: { case_id: number; case_key: string; passed: boolean; checks: Record<string, boolean>; answer: string; tools_called: string[]; latency_ms: number; run_id: string | null }[];
}

export default function EvalsPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const runs = useLoad(() => api.get<EvalRunRow[]>("/api/evals/runs"));
  const cases = useLoad(() => api.get<EvalCase[]>("/api/evals/cases"));
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"results" | "cases">("results");
  const [promptVersion, setPromptVersion] = useState("");
  const prompts = useLoad(() => api.get<{ default: string; versions: { version: string; characters: number }[] }>("/api/evals/prompts"));

  useEffect(() => {
    const id = selected ?? runs.data?.[0]?.id;
    if (id) api.get<RunDetail>(`/api/evals/runs/${id}`).then(setDetail).catch((e) => setError(e.message));
  }, [selected, runs.data]);

  async function runSuite() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<EvalRunRow>("/api/evals/runs", { suite: "default", prompt_version: promptVersion || null });
      await runs.reload();
      setSelected(r.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setStatus(c: EvalCase, status: string) {
    await api.patch(`/api/evals/cases/${c.id}`, { status });
    cases.reload();
  }

  const m = detail?.metrics;
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>Eval centre</h1>
          <p>Every case runs through the real agent loop (rehearsal mode, so no business data is written) and is scored on its trajectory: were the right tools called, was an approval triggered, did retrieval hit, did the answer pass the evidence check. In CI a pass rate below the threshold blocks the merge.</p>
        </div>
        {admin && (
          <div className="row">
            <label className="row small muted">
              Prompt version
              <select className="select" style={{ width: 160 }} value={promptVersion} onChange={(e) => setPromptVersion(e.target.value)} aria-label="Prompt version used for the eval">
                <option value="">Default ({prompts.data?.default ?? "v1"})</option>
                {prompts.data?.versions.map((v) => <option key={v.version} value={v.version}>{v.version} · {v.characters} chars</option>)}
              </select>
            </label>
            <button className="btn btn-primary" disabled={busy} onClick={runSuite}><Play size={15} /> {busy ? "Running…" : "Run the regression eval"}</button>
          </div>
        )}
      </div>
      <ErrorBanner error={error ?? runs.error} />
      {m && detail && (
        <>
          <div className="grid grid-4">
            <Kpi label="Pass rate" value={pct(m.pass_rate, 1)} sub={`${detail.passed}/${detail.total} · run #${detail.id} · ${time(detail.created_at)}`} />
            <Kpi label="Average evidence coverage" value={pct(m.avg_evidence_coverage, 1)} sub="Share of factual sentences that passed the sentence check" />
            <Kpi label="Latency p50 / p95" value={`${ms(m.latency_p50_ms)} / ${ms(m.latency_p95_ms)}`} sub={detail.model} />
            <Kpi label="Tokens used" value={m.total_tokens.toLocaleString()} sub={`Estimated cost $${m.estimated_cost.toFixed(4)}`} />
          </div>
          {m.diff && (
            <div className="card card-pad row row-wrap">
              <b>Against run #{m.diff.against_run}:</b>
              {m.diff.regressions.length > 0
                ? <span className="badge badge-danger">Regressed {m.diff.regressions.join(", ")}</span>
                : <span className="badge badge-success">No regressions</span>}
              {m.diff.fixes.length > 0 && <span className="badge badge-success">Fixed {m.diff.fixes.join(", ")}</span>}
              {m.diff.new_cases.length > 0 && <span className="badge badge-info">New {m.diff.new_cases.join(", ")}</span>}
            </div>
          )}
          <div className="card card-pad row row-wrap" style={{ gap: 16 }}>
            {Object.entries(m.checks).map(([k, v]) => (
              <span key={k} className="small">{CHECK_TEXT[k] ?? k} <b>{pct(v)}</b></span>
            ))}
            <span className="spacer" />
            {Object.entries(m.by_tag).map(([k, v]) => <span key={k} className="badge">{k} {pct(v)}</span>)}
          </div>
        </>
      )}
      <div className="row">
        <div className="tabs">
          <button className={tab === "results" ? "active" : ""} onClick={() => setTab("results")}>Results</button>
          <button className={tab === "cases" ? "active" : ""} onClick={() => setTab("cases")}>Case library ({cases.data?.length ?? 0})</button>
        </div>
        <span className="spacer" />
        {tab === "results" && runs.data && runs.data.length > 0 && (
          <select className="select" style={{ width: 280 }} value={detail?.id ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
            {runs.data.map((r) => (
              <option key={r.id} value={r.id}>#{r.id} · {pct(r.metrics.pass_rate)} · Prompt {r.metrics.prompt_version ?? "v1"} · {r.triggered_by} · {time(r.created_at)}</option>
            ))}
          </select>
        )}
      </div>
      {tab === "results" ? (
        <div className="card table-wrap">
          {!detail ? <Empty>No eval runs yet{admin ? " — run one from the top right" : ""}.</Empty> : (
            <table className="table">
              <thead><tr><th>Case</th><th>Result</th><th>Checks</th><th>Tool trajectory</th><th>Answer</th><th>Latency</th></tr></thead>
              <tbody>
                {detail.results.map((r) => (
                  <tr key={r.case_id}>
                    <td className="mono nowrap">{r.case_key}</td>
                    <td>{r.passed ? <span className="badge badge-success">Passed</span> : <span className="badge badge-danger">Failed</span>}</td>
                    <td style={{ minWidth: 180 }}>
                      {Object.entries(r.checks).map(([k, ok]) => (
                        <span key={k} className={`badge ${ok ? "" : "badge-danger"}`} style={{ margin: "0 4px 4px 0" }}>{ok ? "✓" : "✗"} {CHECK_TEXT[k] ?? k}</span>
                      ))}
                    </td>
                    <td className="small mono">{r.tools_called.join(" → ") || "—"}</td>
                    <td style={{ maxWidth: 360 }} className="small">{r.answer}</td>
                    <td className="mono">{r.run_id ? <Link to={`/traces/${r.run_id}`}>{ms(r.latency_ms)}</Link> : ms(r.latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="card table-wrap">
          <table className="table">
            <thead><tr><th>Case</th><th>Question</th><th>Expected</th><th>Source</th><th>Status</th></tr></thead>
            <tbody>
              {cases.data?.map((c) => (
                <tr key={c.id}>
                  <td className="mono nowrap">{c.key}<div className="faint">{c.role}</div></td>
                  <td style={{ maxWidth: 280 }}>{c.question}</td>
                  <td className="small" style={{ maxWidth: 320 }}>
                    {c.expected_tools.length > 0 && <div>Tools: {c.expected_tools.join(" → ")}</div>}
                    {c.must_include.length > 0 && <div>Must include: {c.must_include.join(", ")}</div>}
                    {c.must_not_include.length > 0 && <div>Must not include: {c.must_not_include.join(", ")}</div>}
                    {c.expect_refusal != null && <div>Refusal: {c.expect_refusal ? "yes" : "no"}</div>}
                    {c.expect_approval != null && <div>Approval: {c.expect_approval ? "required" : "not required"}</div>}
                    {c.expected_doc && <div>Expected document: {c.expected_doc}</div>}
                  </td>
                  <td>{c.source === "flywheel" ? <span className="badge badge-warning">Data flywheel</span> : <span className="badge">{c.source}</span>}</td>
                  <td className="stack">
                    <StatusBadge status={c.status} />
                    {admin && c.status === "candidate" && <button className="btn btn-sm" onClick={() => setStatus(c, "active")}>Activate</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
