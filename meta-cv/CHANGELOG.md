# Changelog

Numbers here are measured. Anything that needs the real model, or a real AuraDB run at size, is marked
**not measured yet** until someone runs it.

## 2026-11-01

### Independent checks: Wikidata evidence and a separate judge model
- **The gap:** a claim the graph did not hold was judged only by the model that drafted it, so its mistakes
  repeated across the draft, the judge and the probe.
- **Wikidata evidence** (`engine/wikidata.py`), for about 20 mapped relations:
  - an exact label, alias or year match accepts the claim (source `wikidata`, with the QID);
  - a mismatch on a single-valued fact corrects it to Wikidata's value, or drops it when there are several;
  - anything else, including Wikidata being down, leaves the decision to the model checks;
  - it runs alongside the judge and probe, which are cancelled when it decides;
  - confirmed facts go to the review queue, never straight into the graph;
  - `WIKIDATA_LOOKUP=0` turns it off.
- **Separate judge model:** `META_JUDGE_MODEL` (optional own base URL and key) runs the judge and blind
  probe. The trace says which model judged and whether it is the drafter.
- **Measured live:**
  - the eight true claims about the seed's people and books are confirmed;
  - "born in Bath", "birth year 1897" and "born in New York" are contradicted with the true value;
  - "located in York" does not pass for Warsaw;
  - about 2 s per new subject, 0.4 s cached.
- **Found and fixed while testing live:**
  - contradictions never fired, because several items share a name;
  - substring matching let "Congress Poland" stand in for "Poland";
  - names were fetched one request per value.
- **Tested** (`tests/test_evidence.py`, 6 tests, mocked Wikidata):
  - match, contradiction and silence;
  - Wikidata being down;
  - accepting what the model could not confirm;
  - correcting what the model got wrong;
  - unmapped relations falling back to the model checks;
  - judge-model routing.
- **Not measured yet:** how often Wikidata decides on real questions, and the token savings when it does.

### Faster checking
- **The gap:** every claim the graph did not hold waited for two model calls, one after the other: the judge
  and the blind probe. Claims were checked strictly one at a time, even when they did not depend on each other.
- **Now** (`engine/parallel.py`):
  - a claim's judge and probe run at the same time;
  - claims that depend on no other node have their model checks started together as soon as the draft is in;
  - acceptance, the trace and conflict checks stay strictly in order;
  - a dependent claim is still checked only after its parent passed;
  - the per-question meter is shared across threads (with a lock), so call counts and budgets stay exact.
- **Setting:** `CHECK_WORKERS` (default 6); `1` restores one-by-one checking.
- **Measured** (scripted model, 0.3 s per call, four independent claims): 2.65 s before, 0.44 s now, same answer.
- **Tested** (`tests/test_parallel_checks.py`, 3 tests):
  - faster, with path and trace order unchanged and every call metered;
  - a dependent node costs no model call when its parent fails;
  - `CHECK_WORKERS=1` gives the same result one by one.
  - All earlier tests pass with parallel checking on.
- **Not measured yet:** the latency gain against the real model API, and whether 6 parallel calls stay
  within its rate limits.

### Type checks on facts
- **The gap:** nothing knew what kind of thing an entity is. "Born in 1813" or "written by Warsaw" went to
  the model judge like any other claim, and could be written into the graph.
- **Now** (`engine/entity_types.py`):
  - **Signatures:** relations have type signatures (person, place, work, organization, year, number).
  - **Types from the graph:** entity types come from explicit `entity_type` facts, or are inferred from the
    facts an entity already takes part in.
  - **Rejected without a model call:** a claim that clearly does not fit, in verification and at the write
    gate (quarantined).
  - **Graph first:** a claim the graph can correct is still corrected before the type check.
- **Tested** (`tests/test_entity_types.py`, 4 tests):
  - type inference, mixed evidence and explicit types;
  - clear-evidence-only rejection, including "1984" as a title and dates with a year;
  - a wrong-type claim dropped with no judge or probe call;
  - the write gate quarantines it.
  - All earlier tests pass unchanged. The seed and the synthetic benchmark facts produce no flags.
- **Not measured yet:** how many model calls it saves on real traffic.

### Time-sensitive facts
- **The gap:**
  - The router flagged questions about the present (`time_sensitive`), but nothing acted on the flag.
  - "Who is the CEO of X" could be answered from a fact of any age, at full confidence.
  - Nothing could record that a fact had stopped being true.
- **Validity periods:** facts can carry `valid_from` / `valid_to`.
  - Stored by both graph stores (SQLite migration; Neo4j edge properties).
  - Set through `/facts` and the write gate, and copied by the snapshot export.
  - New store method `fact_info()` returns `verified_at`, the period and the source.
- **Freshness review** (`engine/temporal.py`), for questions about the present:
  - ended facts are dropped as not current;
  - graph facts older than `FRESH_DAYS` (30) get an "as of" note;
  - model-only claims get a "may be out of date" note;
  - any note caps confidence at medium;
  - such answers are not cached or reused as similar-question state.
- **Every question:** a fact's period is shown with it, so an ended fact is never phrased as current.
- **Faithfulness guard:** dates from notes are allowed in the answer, including in a different format.
- **Tested** (`tests/test_temporal.py`, 5 tests; plus a store-contract test that dates round-trip):
  - the former CEO is dropped and the current one kept;
  - the ended fact is mentioned as ended without tripping the guard;
  - a stale fact is flagged;
  - a model-only claim is flagged;
  - the answer is never served from cache.
- **Not checked yet:** the Neo4j variant of the contract test (it runs when AuraDB credentials are set and `NEO4J_TESTS` is not 0).

### Graph-first answers in the factual tier
- **The gap:** the model chose which facts to claim, and the graph only checked them, so an answer could be
  fully verified but incomplete. "Who are the characters in Pride and Prejudice?" could name two of the four
  stored characters at "high" confidence.
- **Now** (`engine/decompose.py`):
  - **Split:** the question is split into parts (lookups, yes/no checks, and parts that build on an earlier
    part's answer).
  - **Graph first:** each part the graph can answer is answered from the graph, with every stored value.
  - **Draft the rest:** the model drafts only the parts that are left, and those are verified as before.
  - **Report gaps:** any part still unanswered becomes an "unverified" gap that the answer reports, and it
    keeps confidence below "high".
  - **Also used by mixed questions:** their premises go through the same walk.
- **Benchmark ablation:** `no_graph_first`.
- **Tested** (`tests/test_graph_first.py`, 7 tests):
  - a three-part question answered with no draft and no judge or probe call;
  - all four characters returned;
  - only the missing part drafted, and a repeated graph answer not re-checked;
  - an unanswered part reported, and confidence capped at "medium";
  - a yes/no part the graph contradicts comes back corrected;
  - the ablation flag turns it off;
  - defensive plan parsing.
- **Changed:** `test_stream_endpoint` now expects 5 model calls instead of 4 (the split is the new one).
  Every other earlier test passes unchanged. The scripted demo answers its multi-fact example from the graph alone.
- **Not measured yet:**
  - completeness and token cost on real questions. Run `python -m app.bench --ablate full,no_graph_first` with the model.

### Number tracing in the reasoning and consistency tiers
- **The gap:** the arithmetic check only proved `expression = result`. A correct calculation on an invented number passed.
  - Example: `1921 - 1897 = 24` for Einstein's age, when the verified premise says 1879.
  - In aggressive mode no logic check runs, so nothing else caught it.
  - Arithmetic written only in a step's text skipped the check entirely.
- **Now:** every number a step uses must trace to one of:
  - the question or the verified premises;
  - an answer this conversation verified;
  - an earlier machine-checked step;
  - a small everyday constant.

  An untraced number sends the solver back to that step, with the reason. The consistency tier's calculations must trace to the question and the statements they cite.
- **Tested** (`tests/test_number_tracing.py`, 8 tests):
  - the Einstein case is caught in aggressive mode and corrected by the backtrack;
  - arithmetic hidden in text is caught;
  - number words, percentages, rounded earlier results and an earlier turn's verified answer all trace;
  - a consistency calculation with an invented year is rejected.
  - All earlier tests pass unchanged.
- **Not measured yet:** how often it fires on real model output, and its false-positive rate there. Run the benchmark with the model.

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

### Lost-context check for trimmed prompts
- **Keep rules:** never cut facts established in this conversation, facts sharing a word with the question, or each named entity's 5 most relevant facts.
  - An entity's own name no longer counts as "shared": counting it meant every fact about the entity looked relevant and nothing was ever trimmed. This was a bug the new tests found.
- **After verification:** if any claim could not be verified and a cut fact or relation concerns the same entity or relation, the draft is redone untrimmed.
  - Claims the graph supports were checked against the full graph, so trimming cannot have changed them.
- **Follow-ups:** if the question mentions something that only the cut part of the history contains, the full history is used.
- **Recorded per answer:** facts and relations kept and cut, whether the draft was redone and why, and whether the full history was used (`final.compression.context`). The Metrics tab shows how often trimming had to be undone.
- **Tested** (`tests/test_compression.py`): a needed fact that trimming hid is recovered by the redraft; no redraft when every claim verifies; the full history is restored when a follow-up refers to a cut turn.

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
