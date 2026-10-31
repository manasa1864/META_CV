# Changelog

Numbers here are measured. Anything that needs the real model, or a real AuraDB run at size, is marked
**not measured yet** until someone runs it.

## 2026-10-31 (later)

### Keyword fallback: general cue groups, measured on held-out data
- **Rules:** rewritten around general classes of wording: arithmetic words, number words, quantity comparisons, durations, measurements with numbers, logic puzzles, and follow-up markers.
  - Numbers inside names ("Boeing 747", "Python 3.12", "COVID-19") are not quantities.
  - Whitespace and polite lead-ins are normalised.
- **Measured (fallback only, labels by the author):**

  | set | before | after |
  |---|---|---|
  | development (123) | 84.6% | 98.4% |
  | held-out 1 (61; its misses were seen while revising) | 72.1% | 100% |
  | held-out 2 (51; written after the rules were frozen, scored once) | n/a | **88.2%** |

  - Held-out 2 is the honest estimate for new questions.
  - Its 6 misses are kept as known failures in `tests/test_fallback.py`.
- **Robustness:** rewording (case, punctuation, "please", "Could you tell me:", extra spaces) never changes the result across all 235 labelled questions.

### Realistic demo questions
- **Example chips:** multi-part facts, a comparison, facts-then-calculation, a pair of similar calculations, a follow-up chain, and the false premise.
- **`bench_data/realistic.json`:** 16 items of these kinds, answered from the seed graph.
- **Seed:** gains birth years and place hierarchies (England, India, the United States).

### Token compression
- **What it does:** trimmed prompts, answering from a similar question's verified facts, and reuse of a verified calculation. Every kind falls back to the full check when it cannot be trusted.
- **`no_compression`:** a benchmark ablation that switches it off.
- **Measured** (call counts with the scripted model in `tests/test_compression.py`):
  - a similar calculation in balanced mode takes 2 model calls instead of 4;
  - a similar fact question needs no new claim chain;
  - on a graph with 120 irrelevant facts about the entity, the draft prompt shrinks to under half its length.
- **Not measured yet:** token savings on real traffic. Run `python -m app.bench --ablate full,no_compression`.

## 2026-10-31

### Store contract and AuraDB hardening (C0, C2)
- **One contract, both stores:** `tests/test_store_contract.py` runs on SQLite and AuraDB, 9 tests each.
  - It found and fixed several differences: relation names are lower-cased on both; both refuse invalid relation names; re-adding the same fact from the same source no longer inflates `count`.
- **AuraDB store:**
  - one shared driver: connection lifetime below Aura's idle cut-off, acquisition timeout, query timeouts, bounded retry with backoff;
  - clear exceptions: `GraphUnavailable`, plus `GraphLimitError`, which is never retried;
  - unique-name constraints created at startup;
  - transitive search bounded (`PATH_MAX_DEPTH`, default 8);
  - writes batched with `UNWIND` (1,000 rows);
  - lookups: exact, then indexed prefix, then a capped `CONTAINS`;
  - relation names, rules and aliases cached for 60 s, so the whole graph is no longer aggregated on every question;
  - provenance on every edge (`source`, `sources`, `verified_at`, `added_by`, `batch_id`);
  - namespaced labels and tags, so tests, benchmarks and scale runs delete only what they created.
- **Measured:** with credentials present, the AuraDB contract and audit tests pass, and the instance is back to 0 nodes / 0 relationships afterwards.

### A. Taxonomy v2 and routing
- **Risk profile:** the classifier returns weighted risk codes, `needs`, false-premise and time-sensitive flags, and where the profile came from (`llm` or `fallback`).
  - Entity lists are coerced safely, with one retry, then the fallback; classification never crashes a turn.
- **Keyword fallback rewritten:** ranges and dates are not arithmetic; "how many / average / percent" alone are not a calculation; word problems with units or rates, and proofs, are.
  - Shared with the consistency tier.
  - **Measured:** all 13 probe cases from the review route correctly; 85% accuracy on 123 labelled questions (labels by the author; most misses are mixed questions with no calculation cue).
- **Mixed questions:** run the factual tier first and pass its accepted triples to the reasoning tier as the only allowed premises. With no verified premise the turn abstains.
- **Consistency tier:** derived numbers must come back as expressions that pass the arithmetic check, or the question is delegated.
- **`caught` entries:** now `{risk, origin_tier, action, detail}`. A clash between two nodes of one factual answer is `factual.relation_error`, no longer "reasoning".
- **Router:** scores P(correct) − λ·P(hallucinated) − c·P(abstained).
  - Outcomes are weighted by signal quality, and the system's own confidence is excluded by default.
  - `ROUTER_FROZEN` / `JUDGE_MODE` make it read-only.
  - Feedback records an outcome.
- **Entity normalisation and aliases** on every read and write; an ambiguous alias goes to review.

### B. Graph correctness and stop conditions
- **`app.audit`:** server-side Cypher on AuraDB, SQL on SQLite. 10 checks with severities, `--fail-on error`, and `/audit/summary` (cached).
  - **Measured:** finds every crafted problem in the test graph on both stores.
- **Write gate:** every write path goes through `graph_guard.add_fact_checked`. Contradictions, incompatible pairs, cycles in acyclic relations and likely duplicate names are quarantined.
  - Ingest no longer writes unverified facts from untrusted sources.
- **Re-check loop (`app.recheck`):**
  - model-verified claims expire after 24 h;
  - every graph write invalidates the cache entries that depended on the changed triple;
  - state tables are pruned.
- **Faithfulness guard:** names, numbers and extracted claims in the composed answer must come from accepted triples. Otherwise the answer is regenerated once, then the verified facts are printed verbatim.
  - The reasoning final answer must equal the last checked step.
- **Stop conditions:** nothing verified, graph conflict, false premise, unverified premise, per-question budget; plus a global verified-only breaker. They are reported in `final.stopped` and shown in the UI.
- **Not measured yet:** how often each stop fires on real traffic.

### C1. Scale test
- **`app.scale`:**
  - deterministic power-law generator with injected errors of known kinds;
  - measures latency percentiles, load throughput, audit runtime and recall, and end-to-end factual-tier latency;
  - safety on Aura Free: refuses to touch other data, checks plan limits, cleans up in `finally`.
- **Not measured yet on AuraDB.** At a toy size on SQLite the audit finds every injected error; that is a code-path check, not a result.

### D. Benchmark v2
- **Questions moved into `bench_data/`:**
  - the familiar control set (56 turns);
  - a synthetic knowledge base of invented entities (44 items across retrieval, false premise, nonexistent, multi-hop, near-duplicate and follow-up);
  - a `real_world_TODO.json` for sourced items;
  - 123 taxonomy cases.
- **Grading:** claim-level, with forbidden claims matched on word boundaries. Outcomes are correct / abstained / hallucinated / error, plus risk–coverage.
- **Ablations:** no_graph, no_verification, static_router, no_reuse, no_guard, with repeats and Wilson intervals.
- **Router evaluation:** train/test split; the learning curve is labelled in-sample.
- **Results:** written to `results/` and `latest.json`; `--report` prints methodology and caveats.
- **Not measured yet:** needs the Meta API keys.

### E. Deployment
- **One container** (`Dockerfile`): `app.server` serves the built frontend and the API under `/api`.
- **Health and fallback:** `/healthz`, `/readyz`, `/status`; read-only snapshot fallback with a `degraded` flag.
- **Judge mode:** admin token for writes, per-IP rate limit, daily token budget, concurrency cap, sanitised errors (no instance URI or stack trace reaches the browser).
- **LLM client:** shared, with timeout, retry with jitter, and a circuit breaker.
- **Replay mode:** records real runs only (`app.record_replays`), labelled "Recorded run". The scripted demo is labelled "Scripted demo (no model)".
- **UI:**
  - example chips; status banners; stop reasons; risk labels;
  - retry after a dropped stream; an error boundary;
  - the intro is off by default and the choice is remembered;
  - a `/results` page.
- **Tools:** `app.preflight` (local and `--url`), `app.loadtest`, Playwright smoke test.
  - **Measured:** the load test at a small size had 0 lock errors; the concurrent-write unit test (20 threads) had 0 errors.
- **Not measured yet:** the 20-conversation load test against AuraDB; the deployed preflight.

### F. Hygiene
- **`requirements.txt`** trimmed to 9 pinned direct dependencies. A clean virtualenv installs them, imports every module, and passes the SQLite tests.
