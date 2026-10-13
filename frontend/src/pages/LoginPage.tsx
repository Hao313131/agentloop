import { useEffect, useState, type FormEvent } from "react";
import { isDemo } from "../demo/replay";
import { api } from "../api";
import { useAuth } from "../auth";
import { Logo, ROLE_TEXT } from "../components/ui";

interface DemoAccounts {
  accounts: { email: string; name: string; role: string; tenant: string }[];
  password: string | null;
}

const CAPABILITIES: [string, string][] = [
  ["Evidence binding", "Every answer is checked sentence by sentence against the knowledge base and the order data; a sentence whose numbers don't match is flagged or held back."],
  ["Human review", "A refund above the tenant's limit goes to an approval queue, and the approver sees every piece of evidence the agent relied on."],
  ["Corrections that stick", "One correction from a support agent becomes both a knowledge patch and a regression test case."],
  ["Regression evals", "Every change runs the eval set, so it is obvious which case went from passing to failing."],
];

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<DemoAccounts | null>(null);

  useEffect(() => {
    api.get<DemoAccounts>("/api/auth/demo-accounts").then(setDemo).catch(() => setDemo(null));
  }, []);

  async function submit(e?: FormEvent, override?: { email: string; password: string }) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(override?.email ?? email, override?.password ?? password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  const tenants = demo ? [...new Set(demo.accounts.map((a) => a.tenant))] : [];

  return (
    <div className="login">
      <section className="login-intro">
        <div className="row" style={{ gap: 10 }}>
          <Logo size={28} />
          <b style={{ fontSize: 16 }}>LoopDesk</b>
        </div>
        <div className="headline">
          <h1>The customer service desk for AI agents</h1>
          <p className="lead">
            Knowledge answers, order handling and human approval in one place. Every answer traces back to its source, and every correction is kept.
          </p>
          <div className="capabilities">
            {CAPABILITIES.map(([title, text], i) => (
              <div className="capability" key={title}>
                <span className="no">0{i + 1}</span>
                <b>{title}</b>
                <span className="desc">{text}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="foot">Hao Xin · a personal project · every company, order and customer shown is fictional</div>
      </section>
      <section className="login-panel">
        <div className="login-card">
          <div>
            <h1>Sign in</h1>
            <p className="muted" style={{ marginTop: 4 }}>
              {isDemo ? "This is the static demo: pick an account below. The data was recorded from a real backend running offline." : "Sign in with your work email, or pick one of the demo accounts below"}
            </p>
          </div>
          <form className="stack" style={{ gap: 14 }} onSubmit={submit}>
            <label className="field">
              Email
              <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" placeholder="name@company.com" />
            </label>
            <label className="field">
              Password
              <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            {error && <div className="error-banner">{error}</div>}
            <button className="btn btn-primary" disabled={busy || !email || !password} style={{ width: "100%" }}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
          {demo && demo.accounts.length > 0 && (
            <div className="stack">
              <div className="faint">Demo accounts · click to enter; each role sees a different set of screens</div>
              <div className="account-list">
                {tenants.map((tenant) => (
                  <div key={tenant}>
                    <div className="account-group">{tenant}</div>
                    {demo.accounts.filter((a) => a.tenant === tenant).map((a) => (
                      <button key={a.email} className="account" disabled={busy}
                        onClick={() => submit(undefined, { email: a.email, password: demo.password ?? "" })}>
                        <span className="avatar me">{a.name.slice(0, 1)}</span>
                        <span style={{ minWidth: 0, lineHeight: 1.35 }}>
                          <div style={{ fontSize: 13 }}>{a.name} <span className="badge">{ROLE_TEXT[a.role]}</span></div>
                          <div className="faint ellipsis">{a.email}</div>
                        </span>
                        <span className="go">Enter →</span>
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
