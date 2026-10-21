# Case study: one customer's refund, from question to money back

This is an original fictional online retailer (Harbour & Home), not a customer interview or a record of a real deployment. The people, orders and amounts are invented (SYNTHETIC). Every step below can be seen in the [live demo](https://Hao313131.github.io/agentloop/), whose data was recorded from the real backend running offline.

## An ordinary afternoon

Emma's robot vacuum ($1,299) is still in transit and she has changed her mind. She asks "How long does a refund take?", then "Where is my order SO20261008002?", then "SO20261008002 I want a refund". At the same time Jack's Bluetooth speaker has failed, and he simply types "I want to speak to a human".

If customer service were just a chat model, four things would go wrong that afternoon:

1. **The timing is wrong.** The policy says 1–3 business days; the model says "the money will be back in 2 hours", and the customer screenshots it and complains.
2. **The money goes to the wrong place.** The model calls the refund tool itself, and $1,299 goes out with no person involved.
3. **The promise is never kept.** The model says "technical support will contact you within 4 hours", but nobody on the staff side can see that ticket.
4. **One model wobble and everyone is stuck.** Every customer sees "the assistant is busy right now, please try again later or ask for a human agent" — and typing "transfer me to a human" gets the same reply, because handing over was itself a decision the model had to make.

AgentLoop treats those four as design inputs, not as things to remind the model about in a prompt.

## From problem to implementation

| Problem | Design | What you can check |
| --- | --- | --- |
| Wrong timing and wrong amounts | The answer is split into sentences; every number must appear in the evidence and every citation must exist; in strict mode a reply that fails is never sent | The invented "90-day no-questions-asked returns, money back within 2 hours" at the end of a reply is marked in red and the whole reply is judged an evidence conflict |
| The agent refunds on its own | Tools are risk-rated; a refund above the tenant's automatic limit pauses in an approval queue with the agent's evidence pack attached; the requester cannot approve their own request, anything above $500 needs an admin, and a rejection must state a reason | An agent approving $1,299 gets 403; two concurrent refunds for one order execute once |
| Promises nobody keeps | The deadline told to the customer is written to the database when the ticket is created; the staff workbench sorts by it and flags overdue tickets; the outcome goes back into the customer's conversation | Two people claim the same ticket and the second gets 409 |
| Customers stuck when the model fails | "Transfer me to a human" is recognised by a rule, without the model; when both model tiers fail, knowledge questions quote the policy verbatim with a citation and order questions go to a person; each model has its own circuit breaker | Pointed at an unreachable model: about 6 seconds for each of the first two requests, 0 seconds from the third |
| Fix it and it breaks again | One correction from a support agent produces both a knowledge patch and an eval case; a patch that contradicts current policy is refused; the eval set runs in CI and a pass rate below the threshold blocks the merge | The regression gate is 52/52 and 10/10 for the two tenants |
| No way to explain what happened | Every run is one trace (model, tools, approvals, verification); staff approvals, exports and human replies go into an audit log with HMAC signatures and sequential numbering | Edit or delete a record directly in the database and the integrity check finds it |

## The actual AI path

Customer message → PII masking and injection detection → explicit request for a person? hand over directly (a rule, no model call) → follow-up rewriting → route to the fast or strong model by risk → agent loop: search the knowledge base (word tokenisation + BM25 and vector recall, fused with RRF, with IDF-weighted term coverage as the relevance floor), look up the order, track the parcel, request a refund (pre-checks, approval policy) → sentence-by-sentence evidence check → write the conversation and the trace.

When a refund is over the limit, the run saves the message history, the evidence and the pending tool call to the database and pauses. After approval it resumes from the database, re-checks the order status once before executing (in case the order changed while it was queued), executes idempotently against the approval, and puts the result back into the customer's conversation.

Offline, a deterministic rule-based model stands in for the real one (`llm/mock.py`), so CI, the evals and this demo are reproducible and need no API key. Connecting OpenAI, Azure OpenAI, DeepSeek, Gemini or Claude is configuration only (`docs/DEPLOYMENT.md`); before going live, run `python -m app.ops.llm_check` to self-test.

## What it measures today

| Metric | Value | Note |
| --- | ---: | --- |
| Retail tenant regression gate | 52/52 | Includes 6 red-team cases (impersonating an approval, faking a system notice, extracting the system prompt…) |
| SaaS tenant regression gate | 10/10 | A second industry, so the tuning is not fitted to one scenario |
| Hard set | 4/9 | Not in the gate; records what lexical retrieval cannot do |
| One search across 20,000 passages | 391 ms → 10 ms | Inverted index plus a vector matrix; ranking identical to scanning one by one |
| Backend tests | 270+ | Run on SQLite (both trace-write modes) and PostgreSQL |
| End-to-end walkthrough | 4 paths | Playwright drives three roles through cross-role paths |

These numbers come from an offline deterministic model and fictional data. They show that the flow and the rules behave as designed; they say nothing about a real model's answer quality. Evaluating a real model needs an API key, and then the same eval sets run against it.

## The find-the-flaw loop

Scheduled tasks took turns playing a demanding customer, interviewers from Australian tech companies, and a customer service manager, hunting for flaws; four rounds found 37 issues. Every fix started with a test that fails on the old code, then the code change, then CI, then the next round reviewed it. A few that stuck:

- After a refund was rejected, the reply still said "expect the money in 1–3 business days". The sentence was sourced from the policy and the sentence check passed it, but for this customer it was wrong. The verifier now checks against the outcome of that specific refund.
- The first circuit breaker counted "5 failures within 30 seconds". Every test passed. Pointing it at an unreachable address showed that under light traffic, or with a model that hangs, the window never fills. It now counts consecutive failures.
- The erasure right did not cover the customer's own words inside a ticket; that was found while building the ticket workbench.

## What it does not do

- WhatsApp has outbound approval alerts and an inbound conversation channel, but encrypted events (AES) are not implemented yet.
- Paraphrase recall still depends on a synonym list; an unusual phrasing can be stopped by the relevance floor. With semantic embeddings the floor would need recalibrating for the new model.
- Orders and refunds only read and write their own tables; there is no adapter to a real order system or payment gateway.

All of this is on the flaw list as next steps.
