# META CV

An adaptive chatbot that tries to prevent hallucinations by design.
- **Risk profile:** each question gets a profile of the hallucinations it is at risk of.
- **Routing:** it goes to the tier that can verify those risks.
- **Checking:** nothing is said until it has been checked, and the system stops instead of guessing when it cannot verify.
- **Learning:** every outcome is logged, so the router learns which tier works.
- **Live trace:** the UI (Prism, with Bloop) streams every check ("See the thinking"), then the answer and its evidence.

```
META/                        (the repository root)
└── meta-cv/
    ├── backend/             FastAPI app: graph stores, verification engine, LLM client, tools, tests
    ├── frontend/            React + Vite UI: chat, metrics, results page
    ├── bench_data/          benchmark and taxonomy question sets (JSON)
    ├── results/             measured benchmark / scale / taxonomy results (committed)
    ├── replays/             recorded real runs for Replay mode (committed)
    ├── data/                deployment files: graph snapshot, initial state (generated, see docs/DEPLOY.md)
    ├── docs/DEPLOY.md       deployment for judges
    ├── Dockerfile           one container: frontend at /, API under /api
    └── demo_server.py       the pipeline with a scripted stand-in model, labelled "Scripted demo (no model)"
```

All commands run from `meta-cv/backend` unless noted.

## Run it

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env               # fill in AuraDB + META_* (or keep a copy in ~/.meta_cv/.env)
python -m app.seed                   # idempotent starter graph, through the write gate
uvicorn app.main:app --port 8000 --reload
# in frontend/:  npm install; npm run dev   -> http://localhost:5173
```

### Production graph: Neo4j AuraDB

Set `GRAPH_BACKEND=neo4j` and the `NEO4J_*` values from the Aura credentials file.
- **On startup:** the app creates unique-name constraints, which also act as the lookup index.
- **Connections:** it keeps one shared driver, with query timeouts and bounded retry.
- **If Aura is unreachable** (paused, network): it answers read-only from `data/graph_snapshot.db` and says so ("running on a local snapshot").
- **Other state:** everything that is not the fact graph (turns, router outcomes, caches, review queue, quarantine) lives in SQLite at `DB_PATH`, in WAL mode with a busy timeout.

SQLite is also a full graph backend (`GRAPH_BACKEND=sqlite`), used for tests and offline development. `tests/test_store_contract.py` runs the same assertions against both stores. Its AuraDB variant runs only when credentials are present, inside private labels it deletes afterwards.

## How a question is answered

```
question (+ conversation)
  -> exact-repeat cache (re-validated)                                       engine/cache.py
  -> risk profile: weighted risk codes, needs {facts, calculation, history},
     false-premise and time-sensitive flags; keyword fallback if the model fails  engine/router.py
  -> learned router picks the tier                                           engine/router.py
  -> refuse if the last audit found a conflict about these entities           audit.py
  -> tier
       factual      claims verified node by node: graph, then judge + blind probe     engine/factual.py
       reasoning    arithmetic machine-checked, logic checked, backtracking,
                    independent solutions must agree                                  engine/reasoning.py
       consistency  from what the conversation verified; derived numbers must
                    pass the arithmetic check, or it re-asks a standalone question    engine/memory.py
       mixed        facts first, then the calculation from those verified premises   engine/planner.py
  -> faithfulness guard: names, numbers and claims in the answer must come
     from verified facts; regenerate once, else print the verified facts verbatim     engine/guardrails.py
  -> stop conditions, answer, and a `final` event with risks, caught, stopped,
     degraded, tokens and router scores                                               engine/pipeline.py
```

### Taxonomy

Risk codes are adapted from the factuality / faithfulness split in Huang et al., *A Survey on Hallucination in Large Language Models* (arXiv:2311.05232). They are restricted to what this system can check. `COVERS` in `engine/router.py` states which tier verifies which code.

| code | meaning | verified by |
|---|---|---|
| `factual.entity_error` | wrong entity, date or number | factual |
| `factual.relation_error` | right entities, wrong relation | factual |
| `factual.fabrication` | unverifiable invented detail | factual + guard |
| `factual.overclaim` | false premise, certainty beyond evidence | factual |
| `faithful.logic` | bad arithmetic or inference | reasoning |
| `faithful.premise` | calculation on an unverified fact | planner + reasoning |
| `faithful.context` | contradicts earlier turns | consistency |
| `temporal.stale` | may have changed since verified | factual + audit |

### Stop conditions

The system answers "I can't answer this reliably" (and says why) when:
1. **nothing verified:** no claim passed verification;
2. **graph conflict:** the last audit found an error-level conflict about the question's entities;
3. **false premise:** the question's premise is contradicted by the graph;
4. **unverified premise:** a calculation needs a premise that could not be verified;
5. **budget:** the per-question budget (LLM calls, tokens, graph queries) ran out.

A global breaker switches to **verified-only mode** when recent answers are mostly rejections, or when the model or graph keep failing. In that mode only graph facts count, and a banner says so.

### Router learning

Score = P(correct) − λ·P(hallucinated) − c·P(abstained), blended with a prior that favours the specialist tier.
- **Weighting:** outcomes count by signal quality. Benchmark ground truth and user feedback count; the system's own confidence is excluded by default because it is circular.
- **Freezing:** `ROUTER_FROZEN=1` (implied by `JUDGE_MODE=1`) reads the trained state but never changes it.

### Token compression

Fewer tokens, with no check that catches a hallucination skipped (`engine/compression.py`):

| kind | what happens | safety net |
|---|---|---|
| trimmed prompts | the claim draft sees only the ~30 known facts and ~20 relation names most relevant to the question; follow-ups get a compact history | facts about the question's own entities always rank first |
| answer from verified facts | a factual question similar to one answered before is tried against that question's verified facts first | facts re-checked against the graph; answer must pass the faithfulness guard; otherwise the full check runs |
| reuse a verified calculation | a similar calculation adapts the verified steps in one solution | arithmetic machine-checked and logic-checked; if either fails, the full cross-check runs. Trade-off: the independent second solution is skipped |
| exact repeat | the stored verified answer is reused at zero model calls | re-validated against the graph first |

`tests/test_compression.py` counts the model calls each kind saves: two calls instead of four for a similar calculation in balanced mode, and no new claim chain for a similar fact question. Measure the token effect on real traffic with `python -m app.bench --ablate full,no_compression`. The Metrics tab reports average tokens per answer for each kind of reuse.

## Tools

| command | what it does |
|---|---|
| `python -m pytest` | all tests (AuraDB variants run when credentials exist; `NEO4J_TESTS=0` skips them) |
| `python -m app.audit [--fail-on error]` | graph audit: functional violations, incompatible pairs, cycles, near-duplicates, provenance, staleness, relation drift, dangling caches |
| `python -m app.recheck` | re-validate cached paths and reasoning states; drop expired claims; prune old state |
| `python -m app.bench --load-synthetic` | load the fictional benchmark knowledge base (`--unload-synthetic` removes it) |
| `python -m app.bench [--ablate full,no_graph,no_compression,...] [--repeats 3] [--train]` | benchmark (familiar, synthetic, realistic and real-world sets) with Wilson intervals, ablations and router train/test split; writes `results/` |
| `python -m app.bench --report` | methodology, tables and caveats of the latest run |
| `python -m app.bench_taxonomy [--fallback-only]` | classifier accuracy on the development set and two held-out sets, with a confusion matrix |
| `python -m app.scale --backend neo4j --sizes 1000,10000,...` | AuraDB scale test (tagged, cleaned up, refuses to touch other data) |
| `python -m app.loadtest` | 20 concurrent conversations with a scripted model; no lock errors allowed |
| `python -m app.export_snapshot` | read-only SQLite copy of the Aura graph for the fallback |
| `python -m app.record_replays` | record real runs of the example questions for Replay mode |
| `python -m app.preflight [--seed] [--url https://...]` | pre-demo PASS/FAIL checks |
| `npx playwright test` (in `frontend/`) | browser smoke test against the scripted demo |

Deployment, judge mode and the AuraDB pausing plan are in [docs/DEPLOY.md](docs/DEPLOY.md).

## Limitations

- **Similarity reuse is lexical:** content-word overlap, not embeddings. Paraphrases with different wording are not matched.
- **Factual answers depend on graph coverage:** outside the graph, a claim is accepted only if an independent model check and a blind probe agree. That is a model judging a model, so it is weaker than the graph.
- **The benchmark measures what it was built to measure:**
  - the synthetic set is invented;
  - the familiar set's facts are in the seed graph;
  - real-world long-tail items still need to be written with sources (`bench_data/real_world_TODO.json`);
  - keyword grading can miss a fabricated detail that avoids every forbidden term.
- **The keyword fallback is a heuristic.** It is only used when the model's classification fails.
  - It scores 88% on a held-out set written after its rules were frozen (98% on the set it was developed against).
  - Its known misses are listed in `tests/test_fallback.py`.
- **Reusing a verified calculation skips the independent second solution.** That is the price of its token saving; the adapted solution is still machine-checked and logic-checked.
- **AuraDB Free pauses when idle and has node and relationship caps.** The snapshot fallback is read-only and only as fresh as its last export.
- **Learning state is local:** router outcomes and feedback live in one SQLite file. Without a persistent disk they reset on restart (see docs/DEPLOY.md).
