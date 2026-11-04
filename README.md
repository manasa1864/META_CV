# META CV

An adaptive chatbot that tries to prevent hallucinations by design.

**Final evaluation:** [FINAL_REPORT.md](meta-cv/FINAL_REPORT.md) has the measured results, pros and cons, and the comparison with a plain model and typical RAG. Run-by-run reports are in [reports/](meta-cv/reports/INDEX.md).

- **Risk profile:** each question gets a profile of the hallucinations it is at risk of.
- **Routing:** it goes to the tier that can verify those risks.
- **Checking:** nothing is said until it has been checked, and the system stops instead of guessing when it cannot verify.
- **Learning:** every outcome is logged, so the router learns which tier works.
- **Live trace:** the UI (Prism, with Bloop) streams every check ("See the thinking"), then the answer and its evidence.
- **Review page** (`/review`): facts confirmed outside the graph, writes the graph quarantined, and proposed relation rules wait there to be accepted, released, discarded, approved or rejected. Writes follow `ADMIN_TOKEN` / judge mode (`/status` reports `writes`).

```
META/                        (the repository root)
└── meta-cv/
    ├── backend/             FastAPI app: graph stores, verification engine, LLM client, tools, tests
    ├── frontend/            React + Vite UI: chat, metrics, results and review pages
    ├── bench_data/          benchmark and taxonomy question sets (JSON)
    ├── results/             measured benchmark / scale / taxonomy results (committed)
    ├── replays/             recorded real runs for Replay mode (committed)
    ├── data/                deployment files: graph snapshot, initial state (generated, see Deployment below)
    ├── Dockerfile           one container: frontend at /, API under /api
    └── demo_server.py       the pipeline with a scripted stand-in model, labelled "Scripted demo (no model)"
```

All commands run from `meta-cv/backend` unless noted.

## Run it

**1. Get the code**

```bash
git clone https://github.com/manasa1864/META_CV.git
cd META_CV/meta-cv
```

**2. Set up the backend**

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # Windows PowerShell: copy .env.example .env
```

**3. Add your keys to `backend/.env`**

- `META_API_KEY`: your Meta Model API key (required). `META_BASE_URL` and `META_MODEL` are already filled in.
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`: your AuraDB credentials. No AuraDB? Set `GRAPH_BACKEND=sqlite` instead.
- `TAVILY_API_KEY`: optional, from [tavily.com](https://tavily.com), for better web search on new books and pop culture.

`.env` is gitignored, so your keys are never committed.

**4. Start the backend** (in `backend/`, with the virtual environment active)

```bash
python -m app.seed                 # loads the starter facts into the graph (safe to run again)
uvicorn app.main:app --port 8000 --reload
```

**5. Start the frontend** (in a second terminal)

```bash
cd META_CV/meta-cv/frontend
npm install
npm run dev
```

Open http://localhost:5173.

**6. Optional: run the tests and the evaluation** (in `backend/`)

```bash
python -m pytest                   # all tests, no model or network needed
python -m app.report               # the full evaluation, written to report.md and reports/
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
  -> injection tripwires: patterns, then the router's meaning-based rating    safety.py, engine/pipeline.py
  -> exact-repeat cache (re-validated)                                       engine/cache.py
  -> a follow-up that refers back is rewritten as a standalone question       engine/memory.py
  -> risk profile: weighted risk codes, needs {facts, calculation, history},
     false-premise and time-sensitive flags; keyword fallback if the model fails  engine/router.py
  -> learned router picks the tier                                           engine/router.py
  -> refuse if the last audit found a conflict about these entities           audit.py
  -> tier
       factual      graph first: the question split into parts, each part the graph
                    can answer taken from the graph (every stored value); the rest
                    drafted and verified node by node: graph (itself cross-checked
                    on Wikidata), Wikidata, Wikipedia, web quotes (model search
                    + Tavily), then judge + blind probe; unanswered parts reported   engine/decompose.py,
                                                                                      engine/factual.py
       reasoning    arithmetic machine-checked, every number traced to its source,
                    logic checked, backtracking; the level climbs from one solution
                    to cross-checked ones only when a check calls for it            engine/reasoning.py
       consistency  from what the conversation verified; derived numbers must pass
                    the arithmetic check and trace to the cited statements, or it
                    re-asks a standalone question                                     engine/memory.py
       mixed        facts first, then the calculation from those verified premises   engine/planner.py
       explain      open-ended questions the tiers above declined: the model's
                    answer, every calculation in it machine-checked, labelled       engine/explain.py
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

### Plots as ordered events

"What happens in this book" is verified step by step, like any other fact (`engine/plots.py`).

**How a plot is stored:** as events. Each event is an entity named by a short description, with:

| fact | meaning |
|---|---|
| `(event, event_in, work)` | the event belongs to the work |
| `(event, event_order, "n")` | its place in the story |
| `(event, involves, character)` | who takes part |
| `(event, follows, earlier event)` | it comes after the previous event (transitive and acyclic) |

**How plot questions are answered:**
- **Graph first:** the question is split into the lookup `(?, event_in, work)`. That returns every stored
  event, sorted into story order and annotated "event 3 of 9; involves …".
- **Told in order:** the answer is written in that order, from the graph alone.
- **Order claims:** "X happens after Y" is checked along the whole `follows` chain. A claim that reverses the
  order is contradicted, with no model call. The same check works for any acyclic relation, e.g. `located_in`.
- **Type checks:** an event cannot involve a place, or belong to a person.

**How plots get in:** through the write gate, either from the seed (Pride and Prejudice, nine events) or
with `POST /plots` (admin):

```json
{"work": "...", "events": [{"event": "...", "involves": ["..."]}], "source": "..."}
```

The events are listed in story order.

**Limits:** the order is a single main storyline; parallel subplots are not modelled. Plots of books that
are not in the graph are drafted and checked one claim at a time, as before, with no order to check against.

### Independent checks: outside evidence and a separate judge

Outside the graph, a claim used to be accepted only by the same model that drafted it: a judge, plus a blind
probe. That model's mistakes repeat across all three calls. Two things now break that loop.

**Wikidata evidence** (`engine/wikidata.py`). This applies to relations Wikidata records: birth and death
place and year, author, publication date, characters, location, spouse, parents, awards, genre, CEO and others.
1. The subject's item is found by exact name. The mapped property is read, and its values are compared exactly
   with the claim (label or alias, or the year of a date).
2. **The result:**
   - **A match** accepts the claim (source `wikidata`, with the item's QID). It counts as evidence like a graph
     fact, so confidence can be "high".
   - **A mismatch on a single-valued fact** (birth or death place or date) corrects the claim to the value
     Wikidata holds (`wikidata-corrected`). If Wikidata holds several values, the claim is dropped.
   - **Anything else is "unknown":** no exact item, no value, a relation that is not mapped, or Wikidata being
     unreachable. Then the model checks decide, as before. It never raises.
3. **Graph writes:** facts confirmed by Wikidata go to the review queue. A human still decides what enters
   the graph.
4. **Speed:** the lookup runs at the same time as the judge and probe. When it decides, those calls are cancelled.

**A separate judge model.** Set `META_JUDGE_MODEL`, plus `META_JUDGE_BASE_URL` / `META_JUDGE_API_KEY` if it
lives on another API. The judge and blind probe then run on that model. The trace says which model judged,
and whether it is the drafter.

**Measured live** (12 claims about the seed's people and books):
- **True claims:** all eight confirmed.
- **Wrong single-valued claims:** "born in Bath", "birth year 1897" and "born in New York" are each
  contradicted with the true value.
- **No loose matches:** "located in York" does not pass for Warsaw.
- **Speed:** about 2 s for a new subject, 0.4 s cached. Tests use a mocked Wikidata, never the network.

**Privacy:** only the claim's names and the property go to wikidata.org. `WIKIDATA_LOOKUP=0` turns it off.

**Evidence found on the web** (`engine/websearch.py`). No curated database has to hold everything. When a claim or a
plot is in none of the graph, Wikidata, the cache or Wikipedia, the model's own web search (Responses API `web_search`
tool, the same key) finds sources. The model's account of what it read is not trusted as it stands:
1. **One search per question,** made only when something needs it. The model searches, reads pages and answers,
   quoting the sentences it relies on.
2. **Verbatim quotes only.** The app fetches the cited pages itself and keeps only the quotes that occur word for word
   on a page it fetched. Those form the evidence pool; everything else the model said is ignored.
3. **A claim is checked against the pool.** Both its names must occur in the quotes, and an independent model call
   (the judge model, if configured) must find the quotes support it (or contradict it). Then it is accepted (source
   `web`, medium confidence, cited pages, queued for graph review) or dropped. Anything unsettled falls to the model's
   own checks as before.
4. **Plots** use the pool through the same event checks as a Wikipedia article: events must pass a name check and an
   independent check against the quotes.
5. **Safe fetching.** Pages are fetched only from public addresses (no localhost, private or link-local ranges,
   also after redirects), with a size and time limit. Instruction-like sentences are dropped.

If pages cannot be fetched (some sites block bots) the search's own answer is offered, labelled unverified, with its
low confidence. The order for a plot is: graph, Wikipedia, web quotes, the web answer, the model's memory (all labelled),
then a clear decline. Cost: about 10-17 s and ~10k tokens for a question that needs a search, plus the search fee
(listed at $2.50 per 1,000). The question's text is sent to the model API as before. `WEB_SEARCH=0` turns it off.

**Plots from Wikipedia** (`engine/wikipedia.py`). A plot has nothing independent to check: the blind probe cannot
reproduce "Shen Qiao's defeat", because the model made that label up a moment earlier. So when the graph holds no
events for a work, the model is never asked to draft one from memory. Instead:
1. **Find the article.** The work's own Wikipedia article is found. Its title must *be* the work's name (ignoring
   "The" and "(novel)"), or the name must redirect to it. A longer title is a different work ("The Thousand Autumns of
   Jacob de Zoet" is not "Thousand Autumns"). Disambiguation pages and non-story articles are skipped.
2. **List the events.** The model lists the events from the article's plot section, in order.
3. **Check every event.** Each must pass two checks:
   - **Name check:** its names and numbers occur in the article text.
   - **Independent check:** a separate model call (the judge model, if configured) finds it supported by the text.
4. **Answer.** At least 2 events must pass. The answer cites the article and is **medium confidence**: it is the
   model's reading of an article, not a stored fact. It is never cached or saved as a reusable state.

**Fallback, in two steps:**
1. **From memory, labelled.** With no usable article (off, unreachable, not found, no plot section, or too few events
   verified), the model may say what it remembers of the work. The answer starts "Not verified. I couldn't find a
   source for X, so this comes from the model's memory and may contain mistakes". It is low confidence, never cached,
   never saved as a reusable state, and never recorded as a verified fact for follow-up questions. If the model does
   not recognise the work, or is below 60% sure, it says nothing. `PLOT_MEMORY_FALLBACK=0` turns this step off.
   Verified-only mode and questions flagged for injection skip it.
2. **Decline.** Otherwise the app stops with `plot_unavailable`, says why, and says how to add the plot
   (`POST /plots`, or `POST /ingest` with a summary you trust).

A name that redirects to another page counts only if that page has a real plot section ("Manacled" redirects to its
author's page, a biography, so it is not used). Instruction-like sentences in an article are dropped before the model
sees them. `WIKIPEDIA_LOOKUP=0` turns the Wikipedia step off.

**Speed:** Muse Spark thinks at high effort by default (about 30 s for a trivial call). `META_REASONING_EFFORT=low`
is sent on every call: in a measured plot question, 127 s and 32k tokens became 40 s and 11k tokens.

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

### Stored facts are checked, not obeyed

A fact the graph supplies is also looked up on Wikidata, for relations Wikidata records (`GRAPH_CROSSCHECK`, on by default):
- **agreement** is noted on the claim;
- **a contradiction** on a single-valued fact (birth or death place or year) replaces the stored value with Wikidata's and
  queues the stored fact for review. Wikidata may contradict only when its best match is labelled with the exact name, so a
  namesake or a joke entry cannot overwrite a true fact;
- **a value missing from a complete list** (author, director, founder, capital, spouse) is "disputed": not called wrong, but
  not served as verified either. The part goes back to be drafted and checked like any part the graph lacks. Every Wikidata item
  sharing the name is consulted, because the best match is often something else (a village called a hamlet).

Measured on held-out poisoned-fact cases: wrong stored birthplaces, years, places of death, authors and directors were corrected
or declined, and correct stored facts were kept.

### Web evidence from two searches

With `TAVILY_API_KEY` set, Tavily searches at the same time as the model's own web search (`engine/tavily.py`). Its pages join
the evidence pool, and a quote counts if it occurs word for word either in the page the app fetched or in the text Tavily
extracted from that page. Sentences of Tavily's snippets join the pool as quotes; they are page text, not model output, and still
need both names of a claim and the independent judge before they count. Instruction-like sentences are dropped. If either search
fails, the other keeps working.

### Reasoning levels climb automatically

`auto` (the default) starts every calculation at the relaxed level: one solution, every arithmetic step machine-checked and every
number traced. It climbs to balanced (a second independent solution and a logic check), then conservative (up to five solutions
that must strongly agree), only when a check gives a reason:
- a failed check, or a backtrack;
- more than `SIMPLE_SOLUTION_STEPS` (2) steps;
- a calculation built on facts checked elsewhere;
- solutions that disagree.

Each climb, its reason and the tokens spent so far are shown in the trace. If even conservative cannot agree, the answer is
flagged or declined, never loosened. A level picked by hand is used as is. Measured: 17 x 23 is answered at the relaxed level
for 2,352 tokens, against 7,256 at fixed balanced.

### Follow-ups and open-ended questions

- **Follow-ups** that refer back ("where was she born?", "my original $200") are rewritten into a standalone question before
  routing (`memory.rewrite`). The rewrite may only reuse names and numbers the conversation contains; one that adds anything, or
  carries instruction-like text, is rejected. `FOLLOWUP_REWRITE=0` turns it off.
- **Open-ended questions** (a design, an explanation, a comparison) have no single fact to verify, so the verifying tiers used to
  decline them. When the router marks a question `open_ended` and a verifying tier declined it, the explain tier
  (`engine/explain.py`) lets the model write the answer with every calculation marked, machine-checks each one (a wrong result
  is corrected and lowers confidence), and labels the rest as the model's explanation, not verified against sources. It is never
  used for an unfound work or plot, a suspected false premise, a high fabrication risk or a flagged injection. `EXPLAIN_TIER=0`
  keeps the decline.

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

**Measured on the real model** (`python -m app.report --only reuse`, five pairs): 51% fewer tokens on the second question
overall; 100% on exact repeats and 45% on a reworded fact, but 8% and 28% more on similar calculations with new numbers, where
adapting the stored steps adds a logic check the relaxed reasoning level does not need.

`tests/test_compression.py` counts the model calls each kind saves: two calls instead of four for a similar calculation in balanced mode, and no new claim chain for a similar fact question. Measure the token effect on real traffic with `python -m app.bench --ablate full,no_compression`. The Metrics tab reports average tokens per answer for each kind of reuse, and how often trimming had to be undone.

### Safety beyond hallucination

`app/safety.py`, wired into the model client, the write gate, the factual tier, the pipeline and the API:

| risk | what happens |
|---|---|
| prompt injection | every model call appends a rule to its system prompt and wraps its user message in `<data>` tags: the question, graph facts, evidence and earlier turns are material for the task, never instructions. A tripwire for instruction-like text adds a warning step when a question contains it. Facts containing it are quarantined by the write gate, and facts already stored are never shown to the model or used in an answer |
| prompt leaks | an answer that repeats a clause of a system prompt is withheld (stop reason `unsafe_output`) |
| private data | emails, phone numbers, card numbers (Luhn-checked), ID numbers and keys are redacted before a turn is stored. A question containing any is answered but never cached or reused. Chat turns are deleted after `TURNS_RETENTION_DAYS` (90) |
| secrets | the configured keys and passwords are masked in every log record and in the answer stream |
| paraphrased injection | the router's classification also rates `adversarial.injection`; a question rated `INJECTION_RISK_FLAG` (0.6) or more is handled as if the pattern tripwire had fired, at no extra model call. With no pattern to cut out, the model deletes the instruction for the invariance re-run, and its output must be a shortened copy of the question. `SEMANTIC_TRIPWIRE=0` turns it off |
| provider refusals | when the model provider's content policy blocks a request, or the model answers in prose instead of the checkable form, the question is declined with a reason instead of failing with an error |
| abuse | per-IP rate limit always on (30/min, 12 in judge mode), read from the trusted proxy's end of `X-Forwarded-For` so it cannot be dodged by sending a new header value. Questions are capped at 2000 characters. POST/DELETE requests from other sites' pages are refused. Without `ADMIN_TOKEN`, admin writes work only from the machine the app runs on. The production server sends anti-framing and no-sniff headers |

#### Mechanisms built on the verified claims

The table above is standard practice. These four use parts only this system has (verified claim paths, the learned
router, the write gate), so they measure or limit an attack's *effect* instead of only recognising its wording:

| mechanism | what it does | cost |
|---|---|---|
| **instruction-invariance check** (`engine/invariance.py`) | a question the tripwire flags is answered a second time with the instruction-like sentences removed. If the original run verified claims the clean run did not, or reached a different final number, the instructions steered the outcome: the clean answer is used, with an `adversarial.injection` catch and confidence capped at medium. Otherwise the original answer stands | a second run, for flagged questions only; inside the per-question budget |
| **provenance taint** (`safety.user_asserted`) | a claim the question itself states (it names both ends: "Was Austen born in Bath?", "Since Einstein was born in 1880, …") that only model checks back may answer this question. It is not written to the claim cache, saved states or the graph review queue. One user's premise cannot become another user's verified fact | repeats of such questions re-run their model checks |
| **adversarial routing** (`router.adversarial_policy`) | for flagged questions, a learned choice between answering normally and verified-only mode (graph facts only), scored like the tiers on graded outcomes of flagged turns. It starts verified-only and learns from `bench_safety`'s end-to-end grades and user feedback | flagged questions abstain more until evidence says otherwise |
| **source trust** (`source_trust.py`) | the write gate keeps each source's record: confirmed (graph or Wikidata backs it) or contradicted (contradiction, wrong type, injected text). An untrusted source with trust below 50% after 3 checked facts has its new facts sent to review, even facts nothing can check. For untrusted sources, the gate also checks Wikidata | a Wikidata lookup per untrusted write on a relation it covers |

`python -m app.bench_safety` scores it on `bench_data/safety_cases.json`. Measured without a model:
- **Injection tripwire:**
  - direct attempts caught: 20/20 on the development set, 8/8 on a held-out set written after the patterns were frozen;
  - paraphrased attempts caught: 0/6;
  - benign questions flagged: 0/30.
- **Other checks:**
  - redaction: 8/8 found, with no clean text changed;
  - write gate: 4/4 poisoned facts quarantined;
  - prompt leaks: 3/3 caught, with no false alarm.
- **Poisoning campaign** (a source mixing 3 facts the graph contradicts with 5 invented facts nothing can check):
  - invented facts reaching the graph: 2/5 with source trust, 5/5 without;
  - honest facts held back: 0/5.
  - The two that get through arrive before the third contradiction. This is a constructed scenario that shows the mechanism, not a rate on real traffic.

The instruction-invariance check, taint and adversarial routing are tested with a scripted model (`tests/test_safety_mechanisms.py`). How often real attacks steer the real model is what the end-to-end cases measure.

With a model, it also runs attacks through the full pipeline. In the latest measured run every development and held-out case
ended as specified (held-out 25/25), and no injected instruction reached an answer in any version; see
[FINAL_REPORT.md](meta-cv/FINAL_REPORT.md).

The Metrics tab has a live "Safety checks" card covering:
- questions with instruction-like text or private data;
- stored facts ignored, and new facts quarantined, for carrying instructions;
- answers withheld;
- requests refused (rate limit, other-site posts, admin writes, daily budget).

Each turn records what the checks saw in `turns.safety`. Refusals that never become a turn are counted per day in `safety_events`. The tripwire only makes attempts visible, and paraphrases get past it. The defence is the data rule plus verification: an injected instruction cannot switch the checks off.

## Tools

| command | what it does |
|---|---|
| `python -m app.report [--quick] [--only safety,live,...] [--bench N] [--aura-tests]` | **runs the evaluation and writes `report.md`, case by case** (no overall pass/fail): every AI-safety control with one row per case (injection, write gate, source trust campaign, redaction, prompt leaks, abuse controls), attacks through the real pipeline, each novel mechanism switched off in turn, the 12 live questions with every claim and what confirmed it, per-case classification, and every test by name. **Every run is kept as its own version** in `reports/` (`report-v1.md`, `report-v2.md`, ...; each with its raw data and a small metadata file), never overwritten, and `reports/INDEX.md` lists them with what changed and their headline numbers side by side. `report.md` is a copy of the latest. Use `--label` and `--notes` to name a run, `--only live,e2e` to rerun just some sections (the rest are kept from the latest finished version) and `--scratch` to try the report without saving a version. State goes to a throwaway database; `--quick` makes no model calls |
| `python -m app.report --only compare [--compare-set comparison_hard]` | **head to head:** the same questions to this system, a plain model and a typical RAG chatbot (Wikipedia search, top 3 article leads, no checking), all on the same model and key; graded correct / declined / hallucinated by keywords or, for designs and refusals, by a rubric. Sets: `bench_data/comparison.json` (83 questions, 5 difficulty levels, attacks, 7 conversations) and `bench_data/comparison_hard.json` (20 harder ones) |
| `python -m app.report --only reuse` | tokens saved by the cache and intermediate compression on five pairs of repeated or similar questions |
| `python -m app.bench_compare [--set comparison_hard] [--limit 5]` | the head-to-head on its own, results in `results/` |
| `python -m pytest` | all tests (AuraDB variants run when credentials exist; `NEO4J_TESTS=0` skips them) |
| `python -m app.audit [--fail-on error]` | graph audit: functional violations, incompatible pairs, cycles, near-duplicates, provenance, staleness, relation drift, dangling caches |
| `python -m app.recheck` | re-validate cached paths and reasoning states; drop expired claims; prune old state |
| `python -m app.bench --load-synthetic` | load the fictional benchmark knowledge base (`--unload-synthetic` removes it) |
| `python -m app.bench [--ablate full,no_graph,no_graph_first,no_compression,...] [--repeats 3] [--train]` | benchmark (familiar, synthetic, realistic and real-world sets) with Wilson intervals, ablations and router train/test split; writes `results/` |
| `python -m app.bench --report` | methodology, tables and caveats of the latest run |
| `python -m app.bench_taxonomy [--fallback-only]` | classifier accuracy on the development set and two held-out sets, with a confusion matrix |
| `python -m app.bench_safety [--no-model]` | red-team cases: injection tripwire (development and held-out), redaction, write gate on poisoned facts, prompt leaks, and end-to-end attacks with a model |
| `python -m app.scale --backend neo4j --sizes 1000,10000,...` | AuraDB scale test (tagged, cleaned up, refuses to touch other data) |
| `python -m app.loadtest` | 20 concurrent conversations with a scripted model; no lock errors allowed |
| `python -m app.export_snapshot` | read-only SQLite copy of the Aura graph for the fallback |
| `python -m app.record_replays` | record real runs of the example questions for Replay mode |
| `python -m app.preflight [--seed] [--url https://...]` | pre-demo PASS/FAIL checks |
| `npx playwright test` (in `frontend/`) | browser smoke test against the scripted demo |

Deployment, judge mode and the AuraDB pausing plan are in [Deployment](#deployment) below.

## Deployment

One container serves the built frontend at `/` and the API under `/api` (the same paths the Vite dev
proxy uses), built by the multi-stage `Dockerfile`:

```bash
cd meta-cv
docker build -t meta-cv .
docker run -p 8000:8000 --env-file backend/.env -e JUDGE_MODE=1 -e ADMIN_TOKEN=... meta-cv
```

No host-specific files are in the repo yet. Pick a host from the table below first.

### Environment variables

All configuration comes from environment variables; the frontend never receives a secret, and the
container sets `META_NO_HOME_ENV=1` so nothing reads `~/.meta_cv/.env`.

| Variable | Value |
|---|---|
| `GRAPH_BACKEND` | `neo4j` |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | from the Aura credentials file |
| `META_API_KEY`, `META_BASE_URL`, `META_MODEL` | the model |
| `JUDGE_MODE` | `1` - read-only graph, frozen router, per-IP rate limit, daily token budget |
| `ADMIN_TOKEN` | long random string; write endpoints need header `X-Admin-Token`. Without it, writes work only from the machine the app runs on, so a deployment without a token is not writable by visitors |
| `TRUSTED_PROXY_HOPS` | proxies in front of the app that append to `X-Forwarded-For` (default `1`, right for a typical host). The rate limit reads the client IP that many entries from the right, because the left end is whatever the client sent. `0` if the app is exposed directly |
| `ALLOWED_ORIGINS` | other sites allowed to send POST/DELETE requests (default: the Vite dev server only). The app's own origin is always allowed |
| `DB_PATH` | path on a persistent volume if the host has one (default `/data/meta.db`) |
| `PORT` | the port the host expects (Hugging Face Spaces: `7860`) |
| `META_JUDGE_MODEL` | optional: a different model for the independent checks, so the drafter does not check itself (`META_JUDGE_BASE_URL` / `META_JUDGE_API_KEY` if it is on another API) |
| `TAVILY_API_KEY` | optional: Tavily search API key for a second web search (new and niche pages, JavaScript pages); off when empty. `TAVILY_DEPTH` basic or advanced |
| `GRAPH_CROSSCHECK` | optional: `0` stops checking graph-supplied facts against Wikidata (on by default; needs `WIKIDATA_LOOKUP`) |
| `WIKIDATA_LOOKUP` | optional: `0` turns off the Wikidata evidence lookup (on by default; needs outbound HTTPS to wikidata.org) |
| `WIKIPEDIA_LOOKUP` | optional: `0` turns off reading plots from Wikipedia for works the graph has no events for (on by default; needs outbound HTTPS to en.wikipedia.org) |
| `MAX_TOKENS`, `MAX_LLM_CALLS` | per-question budget (150000 tokens, 40 calls). A question that exceeds it stops with a clear message instead of guessing. A web search alone can use 13-25k tokens |
| `WEB_SEARCH` | optional: `0` turns off the model API's web search as evidence (on by default; ~10-17 s and ~10k tokens per question that needs it, plus the search fee; the app also fetches the cited pages, so it needs outbound HTTPS). `WEB_MAX_PAGES` (5), `WEB_PAGE_TIMEOUT` (8) |
| `PLOT_MEMORY_FALLBACK` | optional: `0` stops the model from telling what it remembers of a work's plot (labelled unverified, low confidence) when no source has it; such questions are declined instead |
| `META_REASONING_EFFORT` | `low` or `minimal` for Muse Spark (default model effort is high: ~30 s per call). Empty for providers that reject the parameter |
| `CHECK_WORKERS` | optional: model checks run at the same time (default `6`); lower it if the model API rate-limits, `1` checks one by one |
| optional | `RATE_LIMIT_PER_MIN` (12 in judge mode, 30 otherwise; `0` = off), `MAX_QUESTION_CHARS` (2000), `DAILY_TOKEN_BUDGET` (2000000), `MAX_CONCURRENT` (8), `LLM_TIMEOUT` (90) |
| `FOLLOWUP_REWRITE` | `1` (default) / `0`: rewrite follow-ups that refer back into a standalone question before routing |
| `SEMANTIC_TRIPWIRE`, `INJECTION_RISK_FLAG` | `1` (default) / `0`, and `0.6`: treat a question the router rates this high for injection as if the pattern tripwire had fired |
| `EXPLAIN_TIER` | `1` (default) / `0`: answer open-ended questions the verifying tiers declined, with every calculation machine-checked and the answer labelled |
| `SIMPLE_SOLUTION_STEPS`, `LOGIC_RISK_STRICT_START` | `2`, `1.1` (off): the reasoning ladder accepts a one-solution answer of at most this many steps at the relaxed level; a router logic-risk weight at or above the second starts at balanced |
| `INVARIANCE_CHECK`, `ADVERSARIAL_ROUTING` | `1` (default) / `0`: re-answer flagged questions without their instructions; learned normal-vs-verified-only policy for flagged questions |
| `SOURCE_TRUST_MIN_FACTS`, `SOURCE_TRUST_FLOOR` | `3`, `0.5`: an untrusted source with at least this many checked facts and trust below the floor has its new facts sent to review |
| `TURNS_RETENTION_DAYS` | chat turns are kept this long, with private data already redacted (default `90`; `0` keeps them). Benchmark turns are kept |

### Where state lives

- **Fact graph:** AuraDB.
- **Everything else** (turns, router outcomes, caches, review queue, quarantine, rule proposals): SQLite at `DB_PATH`.
  - **With a persistent volume:** mount it and point `DB_PATH` at it.
  - **Without one:** the app starts from `data/initial_state.db` if that file is shipped, and every restart resets learning. Keep `JUDGE_MODE=1` (frozen router) so this is visible and harmless. Ship `data/initial_state.db` only from a real run: run the benchmark against a fresh `DB_PATH`, then copy that file.
- **Benchmark results** (`results/`) and **recorded runs** (`replays/`) are committed files, so the results page and Replay mode work with no model and no graph.

### Hosts (check current plans before choosing; they change)

| Host | Builds this Dockerfile | Persistent disk | Notes |
|---|---|---|---|
| Render | yes | paid instances only | free instances sleep after inactivity (slow first request) |
| Fly.io | yes | volumes | needs a card; machines can auto-stop/start |
| Railway | yes | volumes | usage-based billing after the trial |
| Hugging Face Spaces (Docker) | yes | paid add-on | set `PORT=7860`; free Spaces sleep when idle |

### AuraDB Free: pausing and limits

- **Pausing:** an idle free instance pauses after a few days, and can be deleted after a long pause.
  - The app cannot resume it: resume it in the Aura console.
  - Keep it awake with an external scheduler (for example a cron job or uptime monitor) calling `GET https://<host>/api/readyz` every 12 hours.
- **If Aura is unreachable anyway:** the app answers read-only from `data/graph_snapshot.db`, and `/readyz` and the UI say "running on a local snapshot".
  - Refresh the snapshot before deploying: `cd meta-cv/backend && python -m app.export_snapshot`.
- **Plan limits:** the free tier caps nodes and relationships. Read the exact numbers in the console.
  - A write that hits the limit fails with `graph_limit` and is never retried.
  - `python -m app.scale` measures where the free tier stops being suitable.

### Before the demo

```bash
cd meta-cv/backend
python -m app.preflight --seed          # graph reachable, seed loaded, audit clean
python -m app.record_replays            # real runs of the example questions, for Replay mode
python -m app.export_snapshot           # read-only fallback copy of the graph
# deploy, then:
python -m app.preflight --url https://<host>
```

`preflight --url` checks:
- `/healthz` and `/readyz` (graph reachable and not degraded);
- that every example question returns a final answer;
- that `/results` loads and `results/latest.json` parses;
- that writes are blocked in judge mode;
- that the audit is clean.

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
- **Reusing a verified calculation skips the independent second solution,** and since the automatic reasoning ladder made fresh solving cheap, adapting stored steps measured 8% to 28% more tokens on simple sums. The adapted solution is still machine-checked and logic-checked.
- **The pattern tripwire misses rewording** (0/6 paraphrases in the red-team cases). The meaning-based tripwire is meant to catch those, but its real-model catch rate and false-alarm rate are not measured yet.
- **It costs more and declines more than a plain chatbot.** Head to head on the same model, a plain model was correct on 82 of 83 questions and this system on 73, at 12 to 28 times the tokens and about 8 times the time; the gap is mostly declines when it cannot verify. On a strong model there is little hallucination left to catch; the design pays off where a wrong answer costs more than no answer.
- **No harmful-content filter:** the app decides how to answer a question, not whether it should. A request for dangerous content is still verified like any other question.
- **AuraDB Free pauses when idle and has node and relationship caps.** The snapshot fallback is read-only and only as fresh as its last export.
- **Learning state is local:** router outcomes and feedback live in one SQLite file. Without a persistent disk they reset on restart (see [Deployment](#deployment)).
