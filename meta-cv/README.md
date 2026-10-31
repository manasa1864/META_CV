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
       factual      graph first: the question split into parts, each part the graph
                    can answer taken from the graph (every stored value); the rest
                    drafted and verified node by node: graph, then judge + blind
                    probe; unanswered parts reported                                  engine/decompose.py,
                                                                                      engine/factual.py
       reasoning    arithmetic machine-checked, every number traced to its source,
                    logic checked, backtracking, independent solutions must agree    engine/reasoning.py
       consistency  from what the conversation verified; derived numbers must pass
                    the arithmetic check and trace to the cited statements, or it
                    re-asks a standalone question                                     engine/memory.py
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
| `temporal.stale` | may have changed since verified | factual (freshness review) + audit |

### Stop conditions

The system answers "I can't answer this reliably" (and says why) when:
1. **nothing verified:** no claim passed verification;
2. **graph conflict:** the last audit found an error-level conflict about the question's entities;
3. **false premise:** the question's premise is contradicted by the graph;
4. **unverified premise:** a calculation needs a premise that could not be verified;
5. **budget:** the per-question budget (LLM calls, tokens, graph queries) ran out.

A global breaker switches to **verified-only mode** when recent answers are mostly rejections, or when the model or graph keep failing. In that mode only graph facts count, and a banner says so.

### Faster checking

A claim the graph does not hold costs two model calls: the independent judgment and the blind probe.
Before, every such claim waited for both calls in turn. Now (`engine/parallel.py`, `factual.py`):
- **Judge and probe together:** a claim's judge and blind probe run at the same time. They never see
  each other's answer, so nothing about the check changes.
- **Independent claims in parallel:** claims that depend on no other node have their model checks started
  together as soon as the draft is in. Whatever the other nodes turn out to be, those checks are needed anyway.
- **Order is unchanged:** nodes are still awaited, accepted, shown and conflict-checked one by one, in order.
- **Dependent claims wait:** a node that depends on another is checked only after that node has passed,
  so no call is spent on a node that will be skipped.

**Measured** with the scripted model, each call taking 0.3 s, on four independent claims: 2.65 s one by
one, 0.44 s in parallel, with the same answer.

**Settings:**
- `CHECK_WORKERS` (default 6) is the size of the shared pool. Lower it if the model API rate-limits you;
  `1` restores one-by-one checking.
- Every parallel call is counted on its question's meter, so the per-question budgets still hold.

### Type checks

`engine/entity_types.py` rejects claims whose subject or object is the wrong kind of thing, for example
"Jane Austen born in 1775" (a year where a place belongs) or "Pride and Prejudice written by Warsaw"
(a place where a person belongs).

**Signatures:** `SIGNATURES` says what each common relation connects:
- `born_in`: person → place
- `birth_year`: person → year
- `written_by`: work → person
- `located_in`: place → place
- `ceo_of`: person → organisation, and others

**Where an entity's type comes from:** the graph, never the model.
- An explicit `(x, entity_type, person)` fact wins.
- Otherwise it is inferred from the facts the entity already takes part in: the subject of `born_in` is a
  person, the object of `located_in` is a place.
- Mixed evidence means "unknown".

**When a claim is rejected:** only on clear evidence.
- A year slot gets something with no year in it.
- An entity slot gets something the graph types differently.
- A person or place slot gets a bare number.

Unknown types pass, relations without a signature pass, and titles that look like years ("1984") are
fine where a work belongs.

**Where it runs:**
- **Claim verification:** after the graph lookup and before any model call. A wrong-type claim the graph
  knows nothing about is dropped at once (`factual.relation_error`), with no judge or probe call. A claim
  the graph can correct is still corrected first.
- **The write gate:** wrong-type facts are quarantined instead of written.

The seed and the synthetic benchmark set pass with nothing flagged. To cover more relations, add them to
`SIGNATURES`.

### Time-sensitive facts

Facts that change (offices, CEOs, records) can carry a validity period: `valid_from` / `valid_to` as `YYYY`,
`YYYY-MM` or `YYYY-MM-DD`, where an empty `valid_to` means still true. Set them through `/facts`, the write
gate or `store.add_fact(..., valid_from=, valid_to=)`. Both stores keep them, and `export_snapshot` copies them.

Every accepted graph fact with a period carries it into the answer ("from 2015 until 2021"), so an ended
fact is never phrased as current.

When the router marks a question time-sensitive ("current", "latest", "now"), `engine/temporal.py` reviews
every accepted fact before the answer is written:

| fact | what happens |
|---|---|
| graph fact whose `valid_to` has passed | dropped as not current (`temporal.stale`, dropped). The answer may say it ended, never that it holds |
| graph fact not re-verified within `FRESH_DAYS` (default 30) | kept with "as of <date>"; confidence at most medium |
| claim confirmed only by the model | kept with "from model knowledge, which may be out of date"; confidence at most medium |
| current graph fact verified recently | used normally |

Two kinds of reuse are skipped for time-sensitive questions, because the answer may have changed since:
- the answer is not cached for exact repeats;
- answers from similar questions are not reused.

Limits:
- **Periods are only as good as the data:** a fact stored without `valid_to` counts as still true.
- **No automatic dates:** ingest does not yet extract periods from text.
- **The seed has no time-varying facts.**
- **No point-in-time questions:** "who was CEO in 2015" is answered from the periods in the text, not by
  filtering on the date.

### Graph-first answers

Drafting first and checking second has a blind spot: the model chooses which facts to claim, so an answer can be
fully verified and still incomplete. Two characters named, four in the graph, and "high" confidence anyway.
So the factual tier asks the graph first (`engine/decompose.py`):
1. **Split:** one model call turns the question into parts. Each part is a triple with one unknown (a lookup),
   or none (a yes/no check). `#n` refers to an earlier part's answer, e.g. `(#1, born_in, ?)` for "where was
   the author born".
2. **Fill from the graph:** a lookup returns *every* stored value for that part. A yes/no part the graph
   contradicts comes back with the stored value (corrected), and the false-premise stop condition sees it.
3. **Draft only the rest:** the model is shown what the graph already answered and asked only for the remaining
   parts. Its claims are verified node by node as before. If every part came from the graph, there is no draft
   and no judge or probe call at all.
4. **Coverage check:** a part that still has no verified answer becomes an "unverified" node. The answer says
   it could not verify that part, and confidence cannot be "high".

A drafted claim that clashes with a graph answer is dropped. One that repeats a graph answer is not
re-checked.

Costs and limits:
- **One extra model call** per factual question (the split). It saves the draft and every judge and probe
  call when the graph covers the question.
- **Lookups follow direct edges only:** "which continent is Warsaw in" finds Poland, not Europe. Yes/no
  checks do use transitive relations.
- **Matching depends on relation names:** a drafted answer counts for a part only if its relation matches
  the part's (same name, or a shared word). The drafter is told to keep the part's relation names.
- **Measure it:** `python -m app.bench --ablate full,no_graph_first`.

### Number tracing

The arithmetic check proves `expression = result`. It cannot see that `1921 - 1897 = 24` uses the wrong birth year.
So every number a reasoning step uses must come from one of these sources (`mathcheck.untraced_number`):
- the question, or the verified premises handed to it;
- an answer this conversation already verified;
- an earlier machine-checked step;
- a small everyday constant (0-10, 12, 24, 52, 60, 100, 360, 365, 366, 1000, 3600).

Number words ("twelve", "a dozen", "half") count, and a source percentage also counts as 0.15, 1.15 or 0.85.
A number stated in a step's text with no checked expression must trace too, so arithmetic cannot hide in prose.
A number from a *similar* problem's verified steps does not count: those steps are only a template.
An untraced number is a contradiction like any other: the solver goes back to that step.
This runs in every mode, including aggressive, where the model logic check is off.

Trade-off: an invented number that happens to equal an everyday constant (say 7) still passes.

### Router learning

Score = P(correct) − λ·P(hallucinated) − c·P(abstained), blended with a prior that favours the specialist tier.
- **Weighting:** outcomes count by signal quality. Benchmark ground truth and user feedback count; the system's own confidence is excluded by default because it is circular.
- **Freezing:** `ROUTER_FROZEN=1` (implied by `JUDGE_MODE=1`) reads the trained state but never changes it.

### Token compression

Fewer tokens, with no check that catches a hallucination skipped (`engine/compression.py`):

| kind | what happens | safety net |
|---|---|---|
| trimmed prompts | the claim draft sees the ~30 known facts and ~20 relation names most relevant to the question; follow-ups get a compact history | **keep rules:** facts from this conversation, facts sharing a word with the question, and each named entity's top facts are never cut. **lost-context check:** if a claim cannot be verified and a cut fact or relation concerns the same thing, the draft is redone untrimmed; if a follow-up mentions something only in the cut history, the full history is used |
| answer from verified facts | a factual question similar to one answered before is tried against that question's verified facts first | facts re-checked against the graph; answer must pass the faithfulness guard; otherwise the full check runs |
| reuse a verified calculation | a similar calculation adapts the verified steps in one solution | arithmetic machine-checked and logic-checked; if either fails, the full cross-check runs. Trade-off: the independent second solution is skipped |
| exact repeat | the stored verified answer is reused at zero model calls | re-validated against the graph first |

`tests/test_compression.py` counts the model calls each kind saves: two calls instead of four for a similar calculation in balanced mode, and no new claim chain for a similar fact question. Measure the token effect on real traffic with `python -m app.bench --ablate full,no_compression`. The Metrics tab reports average tokens per answer for each kind of reuse, and how often trimming had to be undone.

## Tools

| command | what it does |
|---|---|
| `python -m pytest` | all tests (AuraDB variants run when credentials exist; `NEO4J_TESTS=0` skips them) |
| `python -m app.audit [--fail-on error]` | graph audit: functional violations, incompatible pairs, cycles, near-duplicates, provenance, staleness, relation drift, dangling caches |
| `python -m app.recheck` | re-validate cached paths and reasoning states; drop expired claims; prune old state |
| `python -m app.bench --load-synthetic` | load the fictional benchmark knowledge base (`--unload-synthetic` removes it) |
| `python -m app.bench [--ablate full,no_graph,no_graph_first,no_compression,...] [--repeats 3] [--train]` | benchmark (familiar, synthetic, realistic and real-world sets) with Wilson intervals, ablations and router train/test split; writes `results/` |
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
