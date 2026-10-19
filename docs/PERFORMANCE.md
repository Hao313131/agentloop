# Performance baseline

## How to measure

```bash
cd backend
python -m app.evals.load_bench --concurrency 1,4,16 --duration 8 --cache off --report perf.json
python -m app.evals.load_bench --database-url postgresql+psycopg://... --workers 4   # PostgreSQL + multi-process
```

The script starts a real uvicorn service (offline model, isolated database), logs in, and calls `POST /api/chat` continuously at each concurrency level, reporting requests per second (RPS), latency percentiles, and error rate. It rotates through 6 questions: knowledge Q&A, order lookup, logistics lookup, and so on.

**What it measures**: the offline model returns almost instantly, so this measures **platform overhead excluding the model** — routing, retrieval, tools, evidence verification, trace writes, and the database. With a real model, a single model call usually takes 1–3 seconds and dominates latency, so the table below should be read as "the ceiling the platform can sustain", not a live prediction.

## Results

Environment: Apple M1 Max (10 cores), Python 3.12, single uvicorn process, SQLite file database, offline model, 8 seconds per level, 0% error rate.

| Scenario | Concurrency | RPS | p50 | p95 | p99 |
|---|---|---|---|---|---|
| Baseline (SQLite default config) | 1 | 94.9 | 10.2ms | 12.4ms | 14.7ms |
| | 4 | 95.5 | 20.1ms | 141.2ms | 260.1ms |
| | 16 | 66.8 | 58.7ms | 651.6ms | 1366.5ms |
| WAL + busy_timeout | 1 | **143.1** | 6.7ms | 8.8ms | 11.3ms |
| | 4 | **141.6** | 20.9ms | 63.1ms | 123.7ms |
| | 16 | **93.6** | 55.1ms | 402.4ms | 754.3ms |
| WAL + answer cache (repeated FAQs) | 1 | 180.2 | 4.7ms | 8.3ms | 9.7ms |
| | 4 | 176.2 | 16.8ms | 51.6ms | 111.2ms |
| | 16 | 109.6 | 39.2ms | 300.7ms | 738.1ms |

## Analysis

1. **SQLite's single-writer lock is the first bottleneck.** Each conversation writes the run record, several spans (each committed on completion so the trace page is live), and messages — about 8 to 10 commits per request. In the default rollback-journal mode, reads and writes block each other. WAL lets reads not block writes, and `synchronous=NORMAL` trims fsyncs, lifting throughput 40%–50% and roughly halving p99 at concurrency 16 (`_sqlite_pragmas` in `app/db.py`).
2. **A single process plus the GIL is the second bottleneck.** Going from concurrency 1 to 4 barely moves throughput: tokenisation, BM25 (pure-Python passage scanning), and evidence verification are all CPU-bound Python, running in a thread pool so multithreading can't use multiple cores. At concurrency 16, lock waits and thread switches appear and throughput actually drops while tail latency rises.
3. **Answer cache** skips retrieval and the model call, lifting throughput another ~25% on repeated FAQs.

## Optimisation: tokenisation result cache (adopted)

Profiling (cProfile, 120 conversations in one process) showed tokenisation was about 15% of per-request CPU: one answer tokenises the same knowledge sentences several times (retrieval, answer extraction, evidence verification each once). Tokenisation is a pure function for the same text and dictionary, so an LRU cache (4096 entries) was added for text under 2000 characters; long document text at ingest is not cached, keeping memory bounded. The cache returns copies so callers mutating a list can't corrupt it.

| Concurrency (answer cache off, measured twice each) | Before RPS | After RPS | p50 | p99 |
|---|---|---|---|---|
| 1 | 141.1 / 141.2 | **161.5 / 162.8** (+15%) | 6.9 → 6.0ms | 9.8 → 9.7ms, flat |
| 4 | 136.8 / 138.1 | **157.2 / 158.5** (+15%) | 21.4 → 17.2ms | 117–136 → 133–140ms, within noise |
| 16 | 87.1 / 90.1 | **103.4 / 96.6** (+11%) | 59 → 46ms | 863–946 → 749–845ms |

At concurrency 4 the p99 ranges overlap, so no improvement is claimed there; once throughput rises, more requests per unit time contend for the SQLite write lock, and tail latency is still dominated by that lock.

## Optimisation: retrieval scaling with knowledge-base size (adopted)

The load test only used the demo knowledge base (~30 passages), hiding how retrieval scales. Added `python -m app.evals.retrieval_bench`: in a temp database, add N synthetic passages to the demo tenant (recombined demo sentences plus invented product names, with vocabulary and length close to real), measuring index rebuild time and each `hybrid_search` call (local Apple M1 Max, offline hash vectors, 20 runs per level).

| Passages | Before p50 / p95 | After p50 / p95 | Rebuild |
|---|---|---|---|
| 1,000 | 19.7 / 20.4ms | **0.6 / 2.4ms** | 0.02 → 0.04s |
| 5,000 | 99.0 / 134.2ms | **2.1 / 4.9ms** | 0.95 → 1.02s |
| 20,000 | 391.5 / 410.5ms | **10.2 / 21.4ms** | 3.6 → 4.0s |
| 50,000 | — | **26.7 / 49.8ms** | 10.5s |

- **Cause**: both recall paths computed passage-by-passage in Python. BM25 looked up term frequency per passage per query word; the vector path did a 512-dimension dot product per passage. Time grew linearly with passage count, about 2 seconds per search at 100k passages.
- **Fix**: BM25 moved to an inverted index so a query only touches passages containing query words; vector scores became one matrix multiply (numpy, float32 matrix: 20k × 512 dims about 40MB, versus a Python float list per passage before, several times larger).
- **Results unchanged**: a new test compares the two implementations side by side: BM25 ranks and scores are bit-identical (each passage accumulated in the same order); vector scores differ by under 1e-5 and ranks match. The only difference is float32 near-zero scores: the old implementation treated 1e-9 float noise as "has similarity" and admitted it as a candidate, now anything under 1e-6 is irrelevant. The gate 46/46, retrieval hit@1 and MRR, and threshold probes (33/33, 16/20) were all unchanged.
- **Step two: rebuild after document changes**. After any document change the index rebuilds wholesale on the next search, which that search must wait for. Before, every rebuild was a cold start: 10.5s for 50k passages, mostly re-tokenising everything, then turning stored vectors into Python lists before a matrix. Now the rebuild reuses tokenisation of unchanged passages (re-ingested documents make new passage rows, only they need tokenising), and vectors are built straight from stored bytes. The tokenisation cache key is "passage ID + text hash", because SQLite reuses deleted-row IDs, so ID-only would return another passage's tokenisation (covered by a test that fails on ID-only).

  | Passages | Cold-start rebuild | Rebuild after editing one doc |
  |---|---|---|
  | 5,000 | 0.85s | **0.10s** |
  | 20,000 | 3.5s | **0.42s** |
  | 50,000 | 8.7s (10.5s before) | **1.4s** (10.5s cold before, every time) |

- **Step three: freshness check per search**. Every search (and answer-cache key computation) confirms the index isn't stale. The old approach counted the tenant's passage count, max ID, and summed document versions — 2.63ms at 50k passages, done twice per conversation, and two round trips on PostgreSQL. Switched to a knowledge version number on the tenant: every passage change bumps it in the same transaction, so the check becomes one primary-key lookup — 2.63 → 0.06ms at 50k passages, and single-search p50 20.9 → 17.3ms. The version and passage change commit together, so any worker seeing the new version also sees the new passages.
- **Remaining**: cold start (process restart, new worker) still tokenises all passages; one copy per worker. At 100k+ passages, or with a dense real vector model, swap the vector path for pgvector / Milvus approximate-nearest-neighbour indexing and BM25 for a search engine.

## PostgreSQL and multi-worker (measured on GitHub Actions)

No local PostgreSQL, so it ran on GitHub Actions (the manually triggered `Load benchmark` workflow): on the same 4-vCPU runner, in sequence, SQLite single worker, PostgreSQL single worker, PostgreSQL 4 workers; each config got its own database and warmed up for 5 seconds first. The load client and server share the machine, so multi-worker results include that contention.

| Config (same run, 10s per level) | Concurrency 1 | Concurrency 4 | Concurrency 16 |
|---|---|---|---|
| SQLite (WAL), 1 worker | 90.8 RPS, p99 15ms | 82.7 RPS, p99 188ms | 76.3 RPS, p99 1299ms |
| PostgreSQL, 1 worker | 40.6 RPS, p99 30ms | 49.2 RPS, p99 102ms | 45.1 RPS, p99 470ms |
| PostgreSQL, 4 workers | 14.9 RPS, p99 76ms | 50.2 RPS, p99 94ms | **103.6 RPS, p99 222ms** |

- **Absolute numbers on the shared runner aren't comparable**: between two runs, SQLite concurrency 1 measured 148 and 91 RPS respectively. Only compare configs within one run.
- **PostgreSQL is slower per request**: one request commits about 10 times (each span on completion), each a network round trip plus WAL fsync, so single-worker throughput is under half of SQLite's.
- **But it's steadier under high concurrency**: no whole-database single-writer lock, so at concurrency 16 PostgreSQL single worker's p99 (470ms) is just over a third of SQLite's (1299ms).
- **Multiple workers unlock multiple cores**: PostgreSQL 4 workers hit 2.3× the single-worker throughput at concurrency 16, with p99 halved again — the best of the three. Local SQLite shows the same trend: at concurrency 4, 4 workers beat 1 by 40% (153 → 215 RPS), but SQLite's write lock caps the ceiling.
- **At concurrency 1, 4 workers is slower: the time isn't in the app**. On CI, PostgreSQL 4 workers added a fixed ~41ms per request at concurrency 1, not removed by warmup or a separate database, and not reproducible locally (macOS, SQLite). To locate it, each response got a `Server-Timing: app;dur=…` header (in-app time), and the load test reported both client and in-app percentiles. The third run:

  | Config (concurrency 1) | Client p50 | In-app p50 |
  |---|---|---|
  | PostgreSQL, 1 worker | 15.1ms | 14.4ms |
  | PostgreSQL, 4 workers | 57.1ms | 16.1ms |

  In-app time is nearly identical; the extra ~41ms is entirely outside the app: connection handling between the client and uvicorn's multi-process mode. The fixed ~40ms per request is the classic signature of Nagle's algorithm meeting delayed ACK, though not yet confirmed by packet capture. Production traffic hits a reverse proxy first (connection pool, keep-alives), so this number should be re-measured behind the proxy, not tuned in the app.

- **Conclusion**: deploy on PostgreSQL with multiple workers in production. Dropping only the fsync wait ("async commit") showed no gain; reducing round trips via "batched trace writes" measured 39%–47% and is now the PostgreSQL default (see below).

## Optimisation: batched trace writes on PostgreSQL (adopted)

The async-commit experiment showed the bottleneck wasn't WAL fsync, so first count the SQL a request actually sends: SQLAlchemy event listeners showed one conversation was 23.3 statements and 9.3 commits, plus psycopg's implicit BEGIN per transaction — about 40 round trips on PostgreSQL, mostly from "commit each span on completion" (and the accompanying run-record token update).

Fix: on PostgreSQL, trace writes buffer in memory and commit once at the run's own commit points (end, approval pause, error); per-request commits dropped from 9.3 to 3.0. SQLite still commits one by one, because earlier experiments showed merged writes worsened SQLite's p99. Cost: on PostgreSQL you wait until the run ends to see its spans on the trace page.

Within one CI run (PostgreSQL, 1 worker):

| Trace write mode | Concurrency 1 | Concurrency 4 | Concurrency 16 | In-app p50 (c1) | p99 (c16) |
|---|---|---|---|---|---|
| Per-span commit | 36.5 RPS | 45.0 RPS | 40.4 RPS | 25.5ms | 528ms |
| Batched | **53.5 RPS** | **62.5 RPS** | **57.2 RPS** | 17.1ms | 393ms |

Throughput up 39%–47%. In the same run, PostgreSQL 4 workers hit 129.7 RPS / p99 193ms at concurrency 16.

**A bug CI caught**: after the first push, CI's PostgreSQL job failed 4 tests. On run end, the system judges state (handoff or not) and whether to write the answer cache from the tools used this run, and the tool list was queried from the database's spans; in batched mode those spans were still in memory, so the query returned empty. The fix passes the buffer to the session before querying. To stop relying only on the PostgreSQL job for this class of bug, the SQLite job also re-runs the full suite in batched mode (`LOOPDESK_TEST_TRACE_WRITES=buffered`).

## Experiment: async span commit on PostgreSQL (not adopted)

Hypothesis: PostgreSQL's slow per-request was the ~10 commits each waiting on WAL fsync, most writing only trace spans. So span commits used `SET LOCAL synchronous_commit TO OFF` (business writes stayed synchronous), expecting a clear single-worker throughput gain.

Within one CI run (PostgreSQL, 1 worker, 10s per level, warmed up first):

| Span commit mode | Concurrency 1 | Concurrency 4 | Concurrency 16 | In-app p50 (c1) |
|---|---|---|---|---|
| Synchronous (default) | 38.3 RPS | 46.2 RPS | 41.3 RPS | 24.5ms |
| Asynchronous | 37.0 RPS | 43.5 RPS | 39.4 RPS | 25.4ms |

No gain (within noise, slightly lower). This shows that in this environment PostgreSQL's per-request cost isn't WAL fsync but more likely per-statement network round trips and CPU contention on the same 4-vCPU machine. Async commit trades durability for performance with no payoff, so the code was reverted. Next step is `pg_stat_statements` or server-side segmented timing to find the real cost before optimising.

## Experiment: merged span writes (not adopted)

Hypothesis: committing each span separately was the remaining major cost, so merging into "at most once per 0.25s, plus one commit before executing a tool" should be faster.

| Approach (cache off, measured twice each) | Concurrency 4: RPS | Concurrency 4: p99 | Concurrency 16: RPS | Concurrency 16: p99 |
|---|---|---|---|---|
| WAL only (current) | 143–145 | 118–135ms | 97–99 | 845–923ms |
| Merged span writes | 154–155 | 190–199ms | 96–101 | 942–950ms |

Throughput up ~7%, but **concurrency 4's p99 steadily worsened by ~50%**, and concurrency 16's throughput didn't improve. The reason: merged commits write more per commit and hold the lock longer, so queued requests wait longer. Support chat cares more about tail latency, and merged writes also hurt trace-page timeliness, so **not adopted**. Merged writes are worth reconsidering only after moving to PostgreSQL (row-level locks, no whole-database single writer).

## Next steps (by expected payoff)

| Change | Expected effect |
|---|---|
| Background index rebuild, serving from the old index during rebuild; persist tokenisation results so cold start needn't re-tokenise everything | Rebuild after editing a doc is already down to ~1.4s for 50k passages; cold start is still ~9s |
| Move trace writes to an async queue (batched to separate storage) | Take trace writes off the request path. On SQLite be careful: dropping per-span commits means the run's update transaction holds the write lock until the run ends, effectively wrapping the model call in the lock too; do it on PostgreSQL, or async the run update as well |
| Async IO after connecting a real model (async httpx + async database) | While waiting for the model, the thread is idle; async raises a single process's concurrent suspended conversations from the thread-pool size (~40) to over a thousand |
| Swap vector retrieval for pgvector or Milvus and BM25 for a search engine | After the inverted index and matrix multiply, retrieval is ~27ms at 50k passages; switch to approximate-nearest-neighbour indexing at 100k+ passages with dense vectors |
