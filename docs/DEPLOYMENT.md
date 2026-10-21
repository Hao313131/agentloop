# Deployment and configuration

## Run locally

```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && pnpm install && pnpm dev     # http://localhost:5173
```

To run a single process: run `pnpm build` first, then FastAPI serves the console pages once it finds `frontend/dist` — just open http://localhost:8000.

Reset demo data: `python -m app.seed --reset`.

## Docker

```bash
cp .env.example .env    # edit as needed
docker compose up --build
```

`docker-compose.yml` starts PostgreSQL 16 and the application image (a multi-stage build: Node builds the frontend, Python runs the backend and serves the static files, as a non-root user). Before going live, change `AGENTLOOP_JWT_SECRET` and set `AGENTLOOP_SEED_DEMO=0`. In production config (`AGENTLOOP_SEED_DEMO=0`), if the JWT secret is still the public default from the repo (the code default, `.env.example`, or the placeholder in `docker-compose.yml`) or is shorter than 32 bytes, the service refuses to start; in demo mode it only prints a warning at startup.

Behind a reverse proxy such as Nginx, start with `uvicorn app.main:app --proxy-headers --forwarded-allow-ips=<proxy IP>` so login rate-limiting sees the real client IP; don't let the app trust `X-Forwarded-For` from arbitrary sources, or an attacker can spoof an IP to bypass limiting.

## Health checks and troubleshooting

- `/api/health`: liveness check, only confirms the process is running.
- `/api/ready`: readiness check, confirms the database is reachable, the schema is migrated to the latest version, and the retrieval indexes have been warmed in the background, returning 503 otherwise. Warming means the first customer after a restart doesn't wait for indexing (about 9s for 50k passages); `AGENTLOOP_PREWARM_INDEX=0` disables it, building on first search instead. The `HEALTHCHECK` in the Dockerfile and a Kubernetes readinessProbe should both point here.
- Model failure doesn't affect readiness: with the model down the system still hands off or quotes policy (see ARCHITECTURE "Model failure"), so `/api/ready` doesn't check the model. Failures are caught by alerting: the dashboard top shows the count and cause of runs that errored in the last 15 minutes (model unavailable, request rejected, timeout, breaker open, process interrupted); Prometheus's `agentloop_run_errors_total` counts all errored runs (whether the customer ultimately got a degraded reply or a handoff), so alert like this:

  ```yaml
  - alert: AgentLoopModelErrors
    expr: increase(agentloop_run_errors_total[5m]) > 5
    for: 2m
    annotations:
      summary: "AgentLoop runs are erroring continuously; customers are getting degraded replies or handoffs"
  ```

  "Request rejected" (4xx) is mostly a config or parameter problem (key, model name, `stream_options`) — retrying and waiting won't help, fix the config directly.
- Every response carries `X-Request-ID`. When a user reports a fault, that ID alone finds the request in the logs and the matching agent run in the trace (the trace detail shows request_id).

## External anchors for audit records

After each audit record commits, a structured log line `"msg": "audit anchor"` (with tenant, number, signature) is written. Send the application logs (`AGENTLOOP_LOG_FORMAT=json`) to a log platform the database writer can't alter, and reconcile regularly:

```bash
cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/python -m app.ops.audit_anchor_check exported-app.log   # exits 1 on mismatch
```

`/api/audit/verify` finds rewritten and mid-list deleted records; deleting the newest few along with the counter can only be caught by reconciling against the log anchors.

## Upgrade notes

- **After migration 0013 (knowledge version number), don't let old-version instances keep writing the knowledge base**: an old instance changing passages won't bump the tenant's knowledge version, so new instances keep using the old retrieval index and answer cache until the next version change. Stop the old instance first (or pause knowledge editing and patch publishing during the upgrade), then start the new version.
- **After migration 0015 (audit signatures)**, pre-upgrade audit records have no signature and integrity checks report "unsigned". Run `python -m app.ops.audit_backfill` once after upgrading to backfill them; any unsigned record appearing afterwards means someone wrote directly to the audit table — don't backfill it.

## Channel message compensation

```bash
cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/python -m app.ops.channel_retry   # every minute
```

WhatsApp events are written to the database before acknowledgement. If the process handling one crashes after acknowledgement, WhatsApp won't resend, so this task re-claims and answers it: events not yet started wait 30s, in-progress events wait twice the run deadline (at least 5 minutes) before the original process is deemed gone; each event is tried at most 3 times, then the customer gets the fallback line. Every event is claimed with a conditional update, so multiple instances don't double-process.

## Cleaning up interrupted runs

```bash
cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/python -m app.ops.stale_runs   # every minute, scheduled alongside channel_retry
```

When a process is killed mid-run (kill -9, OOM, container reclaim, or a publish that doesn't wait for the request to finish), none of its exception handling runs and the run record hangs at "running": metrics keep counting it, the trace never ends, and the customer who just got a refund gets no answer. This task marks runs past the cutoff as failed (`interrupted: the process ended before the run finished`, with the end time) and exports an OpenTelemetry trace like a normal finish; if the customer is still waiting (the conversation's latest message is the customer's question, or this run's "refund submitted for review" note), it leaves a system note in the conversation: processing was interrupted, refunds should be judged by order status, and you can resend or hand off. No note is left in a WhatsApp conversation (the channel compensator re-answers that message), nor in a conversation already in handoff or where the customer has already re-asked.

- **Cutoff = run deadline + 5s + 3 × model timeout + 60s** (`AGENTLOOP_RUN_DEADLINE_SECONDS`, `AGENTLOOP_LLM_TIMEOUT`), default 45 + 5 + 180 + 60 = 290s. The run deadline only constrains the agent loop; the pre-loop follow-up rewrite has its own 5s cap; with LLM verification on (`AGENTLOOP_VERIFIER=llm`), the post-loop verification is one model call outside the deadline, tried at most 3 times; the 60s covers tools, commits, and lock waits. A shorter cutoff would mark still-running records failed; a longer one only delays cleanup, so the upper bound is used, counting the LLM verification even when off. The scheduled task must use the same environment variables as the app, since the cutoff is derived from them. `AGENTLOOP_RUN_DEADLINE_SECONDS=0` estimates the loop portion as "max steps × two model tiers × 3 attempts".
- **A run recovering from approval counts from the approval decision time**: on resume the run becomes "running" again, but the run record itself may have been created days earlier. Runs still waiting for approval are not processed.
- **Time-based, not "end all 'running' at startup"**: with multiple workers or instances, another process's in-flight run is also "running". Each run ends with a conditional update (`WHERE status = 'running'` and still past the cutoff), so multiple instances process each run only once and leave only one note.
- **Also runs once at app startup**, cleaning up long-overdue leftovers — e.g. after a long outage restart, or a single-instance deploy with no scheduled task configured (the docker-compose demo has none). The crash that caused this restart may not yet be past the cutoff, so it relies on the scheduled task.

## Data retention

```bash
cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90 --dry-run   # preview first
cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90
```

Recommended once a day via a Kubernetes CronJob or system scheduler. Trace details (spans) past the retention window are deleted; run-record inputs/outputs, conversation text, and closed-ticket titles, descriptions, and outcomes past the window are replaced with a "past retention" marker (open tickets are left alone, someone still needs to handle them); metrics like tokens, cost, latency, and verification verdict, plus approval, refund, feedback, and other audit and flywheel records, are kept; runs still awaiting approval are not processed.

## Database migrations

Schema is managed by Alembic (`backend/migrations`). The app auto-runs `alembic upgrade head` on startup (`AGENTLOOP_AUTO_MIGRATE=1`, on by default).

- After changing models, generate a migration: `cd backend && AGENTLOOP_DATABASE_URL=... .venv/bin/alembic revision --autogenerate -m "description"`. Review it by hand before committing alongside the code. CI's `tests/test_migrations.py` verifies the models and migrations match exactly, so a missing migration fails outright.
- A database created by an older version with `create_all` (no `alembic_version` table): on startup the missing tables are filled in and marked as the latest version.
- For multi-replica deploys, put the migration in a one-off step of the release process (e.g. a Kubernetes Job or initContainer) and set `AGENTLOOP_AUTO_MIGRATE=0` on the app to avoid multiple replicas migrating at once.

## Environment variables

All variables are prefixed with `AGENTLOOP_`. A full example is in [`.env.example`](../.env.example).

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./agentloop.db` | Also supports `postgresql+psycopg://...` |
| `JWT_SECRET` | dev default | Must be changed in production, at least 32 bytes; with `SEED_DEMO=0`, a public default or too-short secret refuses to start |
| `SEED_DEMO` / `SEED_DEMO_TRAFFIC` | `1` / offline `1`, live `0` | Demo tenants, accounts, and prefilled conversations. Prefilled conversations call the model a dozen-odd times, so they default off with a live model; explicitly turning them on runs in the background and won't block startup |
| `LOG_FORMAT` / `LOG_LEVEL` | `json` / `INFO` | Structured logs: one JSON per line, with request_id, tenant_id, user_id, ready for ELK or Loki; set `text` for local debugging |
| `AUTO_MIGRATE` | `1` | Run Alembic migrations on startup; set to 0 for multi-replica deploys, handled by the release step instead |
| `LLM_PROVIDER` | `mock` | `mock` / `openai_compat` / `anthropic` |
| `LLM_PRESET` | — | `openai` / `azure` / `deepseek` / `gemini`, auto-fills the base URL |
| `LLM_BASE_URL` / `LLM_API_KEY` | — | Custom OpenAI-compatible endpoint |
| `LLM_MODEL_STRONG` / `LLM_MODEL_FAST` | `mock-strong` / `mock-fast` | The two tiers used by the router |
| `LLM_STREAM_USAGE` | `1` | Request usage on streaming; set 0 if a vendor rejects stream_options, and the system falls back to estimates |
| `ANTHROPIC_EFFORT` | `low` | Claude's effort tier |
| `PRICES` | sample prices | `{"model": [input, output]}`, unit: AUD per 1M tokens |
| `EMBEDDING_PROVIDER` | `hashing` | `hashing` (offline) / `openai_compat` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | — / `512` | Vector model |
| `EVIDENCE_MODE` | `annotate` | `annotate`: stream and mark unsupported sentences; `strict`: buffer and block |
| `VERIFIER` | `lexical` | `lexical` / `llm` |
| `MIN_RELEVANCE` | `0.3` | Retrieval relevance threshold |
| `MAX_AGENT_STEPS` | `6` | Max model calls per run |
| `RATE_LIMIT_PER_MINUTE` | `30` | Max conversation requests per user per minute |
| `LOGIN_ATTEMPTS_PER_EMAIL` / `LOGIN_ATTEMPTS_PER_IP` | `10` / `30` | Login rate-limiting: max attempts per account (email, case- and whitespace-insensitive) and per client IP per minute, both enforced together |
| `RATE_LIMIT_BACKEND` | `auto` | Where login counts live: `auto` (database on PostgreSQL, shared by workers; process memory on SQLite) / `database` / `memory`. Set `database` to run multiple workers on SQLite |
| `PROMPT_VERSION` | `v1` | Default system-prompt version (see `PROMPT_VERSIONS` in `agent/prompts.py`) |
| `PROMPT_CANARY_VERSION` / `PROMPT_CANARY_PERCENT` | — / `0` | Canary: stably assign a share of conversations to the candidate version by session hash; set percent back to 0 to roll back |
| `ANSWER_CACHE_TTL_HOURS` | `24` | Answer-cache lifetime; 0 disables; the cache auto-invalidates on knowledge or config change |
| `REFUND_WINDOW_DAYS` / `REFUND_WINDOW_MAX_COUNT` / `REFUND_WINDOW_MAX_AMOUNT` | `30` / `3` / `200` | Cumulative refund risk control: within the window, if a customer's refund count or cumulative amount exceeds the cap, even a small refund goes to human review |
| `RUN_DEADLINE_SECONDS` | `45` | Whole-run time budget (all steps, both tiers, all retries combined), and the longest a customer waits when the model hangs. Checked before each step; model-call timeout tightens to the remaining time and isn't retried past it. On timeout, hand off (file a ticket, show queue position) and log a `run_deadline` span. 0 disables. The interrupted-run cleanup cutoff is also derived from it (see above) |
| `LLM_BREAKER_FAILURES` | `3` | Breaker threshold per model: opens after this many consecutive failures, and for `LLM_BREAKER_COOLDOWN_SECONDS` (default 60) stops calling that model, so the customer gets a degraded reply (quote policy or hand off) directly; then one probe call is let through and only closes on success. Any success in between resets the count. 0 disables. Breaker state is per-process |
| `OTLP_ENDPOINT` / `OTLP_SERVICE_NAME` | — / `agentloop` | When set, each run exports as an OpenTelemetry trace to this OTLP/HTTP address (e.g. `http://otel-collector:4318/v1/traces`), named per GenAI semantic conventions; only metadata, no question, answer, or tool parameters |
| `AUDIT_KEY` | derived from `JWT_SECRET` in demo mode | HMAC signing key for audit records. In production config (`SEED_DEMO=0`) it must be set separately, at least 32 bytes, and different from the JWT secret, or startup refuses: deriving it from the JWT secret means rotating the JWT key would fail all old records. Keep it apart from the database: whoever can write the DB shouldn't also hold it. Old records fail verification after a key change (rotation not yet supported) |
| `TRACE_WRITES` | `auto` | Trace write mode: `auto` batches on PostgreSQL (writes once at run end, approval pause, or error — measured +39%–47% same-run throughput) and commits each span on completion on SQLite; also `per_span` or `buffered`. In batched mode, the run's trace is only visible after the run ends |
| `WHATSAPP_APPS` | — | WhatsApp (Cloud API): one app per tenant: `{"<tenant slug>": {"phone_number_id", "access_token", "verify_token"}}` |
| `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_VERIFY_TOKEN` + `WHATSAPP_TENANT` | — | Older single-app form for connecting one tenant; you must set `WHATSAPP_TENANT` to say which tenant it belongs to |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook for approval alerts |

## Connecting a hosted model

| Vendor | `LLM_PRESET` | Base URL | Where to get the model ID |
|---|---|---|---|
| OpenAI | `openai` | `https://api.openai.com/v1` | The OpenAI dashboard's model list |
| Azure OpenAI | `azure` | `https://<resource>.openai.azure.com/openai` | Your deployment name, set as `LLM_MODEL_STRONG` / `LLM_MODEL_FAST` |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` | The DeepSeek platform's model list |
| Google Gemini | `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | The Gemini API model list |

Anything else that speaks the OpenAI chat-completions shape (Bedrock's OpenAI-compatible endpoint, OpenRouter, a local vLLM or Ollama server, an in-house gateway) needs only `LLM_PROVIDER=openai_compat` plus `LLM_BASE_URL` and `LLM_API_KEY` — no preset required.

Run a model self-check before going live:

```bash
python -m app.ops.llm_check
```

It sends one minimal request to each of the fast and strong tiers, showing the time on success; on failure it gives a categorised reason and fix advice: 401/403 is key or permission, 404 is model name or address, 400/422 is an unsupported parameter (e.g. `stream_options`, set `AGENTLOOP_LLM_STREAM_USAGE=0`), 5xx and 429 are the other side unavailable or rate-limited, and timeout is network or address. Either tier failing exits 1, so it can sit in a deploy pipeline. Service startup and readiness don't depend on the model (an unavailable model hands off or quotes policy), so the self-check is a separate step.

Model IDs change often, so they aren't hard-coded — copy the latest value from the console. The fast and strong tiers can be different specs from one vendor, or just one configured (`MODEL_FAST` defaults to `MODEL_STRONG`).

## WhatsApp (Cloud API)

1. In the Meta App dashboard, add the WhatsApp product and request the needed permissions (e.g. `whatsapp_business_messaging`).
2. Set the webhook callback URL to `https://<your domain>/api/channels/whatsapp/<tenant slug>/events`, subscribing to the message webhook.
3. **Leave End-to-End Encryption off** (the current version doesn't support encrypted events yet).
4. Put this tenant's Phone Number ID, access token, and verify token into `AGENTLOOP_WHATSAPP_APPS`, e.g. `{"harbour": {"phone_number_id": "...", "access_token": "...", "verify_token": "..."}}`.

**One WhatsApp app per tenant**. Events are verified only against that tenant's verify token, and the `app_id` in a schema-2.0 event must also be that tenant's, so sending an event with the Harbour & Home store's token to `/whatsapp/rostr/events` returns 403 and won't create a user or conversation under the Rostr Cloud docs; replies and approval results also go out from that tenant's own app (the access token is cached per app). A tenant with no app configured rejects the event (failure closes it) in non-demo environments. Two tenants sharing one app_id, or the old single-app variables without `AGENTLOOP_WHATSAPP_TENANT`, fail to start rather than silently letting one credential serve all tenants. A body that isn't a JSON object returns 400, and events with malformed fields are ignored, so no 500 makes WhatsApp resend repeatedly.

On receiving a message, the service returns 200 immediately (WhatsApp demands a response within 3 seconds) and then runs the agent in the background and replies to the original message. The same `event_id` is processed only once. When a refund needing approval completes, the result is pushed proactively to the original chat. Exceptions in the background task are no longer silently swallowed: they're logged at ERROR level to `app.channels.whatsapp` (with message_id and stack) and the user gets the "smart assistant is temporarily busy" fallback line.

## Slack approval alerts

Add an Incoming Webhook to a Slack channel and put the value after `key=` in the webhook URL into `AGENTLOOP_SLACK_WEBHOOK_URL`. Each time an approval is created, the channel receives a Markdown alert. When unset, it's only recorded in the trace (dry run).

## MCP

```bash
cd backend
AGENTLOOP_MCP_USER=agent@harbour.demo .venv/bin/python -m app.mcp_server
```

Claude Desktop example config:

```json
{
  "mcpServers": {
    "agentloop": {
      "command": "/path/to/agentloop/backend/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/agentloop/backend",
      "env": { "AGENTLOOP_MCP_USER": "agent@harbour.demo" }
    }
  }
}
```

MCP exposes only read-only tools (knowledge retrieval, order lookup, logistics lookup) and inherits the permissions of the `AGENTLOOP_MCP_USER` account. Write operations like refunds and tickets are not exposed.
