# META CV

An adaptive chatbot that prevents hallucinations by design. Every question is classified by the
hallucination it is most at risk of, routed to the tier built to prevent that type, checked
before it is said, and logged so the router learns which tier actually works. The UI streams
each step live ("See the thinking"), says when processing is complete, then gives the answer.

- **Factual** risk → graph tier: claims are checked one by one against the knowledge graph, so a wrong fact stops the chain instead of spreading.
- **Reasoning** risk → reasoning tier: arithmetic is machine-checked, logic is checked independently, contradictions trigger backtracking, and independent solutions must agree (conservative / balanced / aggressive).
- **Consistency** risk → consistency tier: follow-ups are answered from what the conversation already verified, and new claims that contradict earlier turns are dropped.
- **Learned routing**: each tier's track record per risk type (benchmark grades, your Yes/No feedback, or whether the answer came out fully verified) decides which tier is used next.
- **Reasoning reuse**: exact repeats reuse a re-validated verified path; similar questions get a verified state (triples or checked steps) injected to adapt instead of rebuilding.
- **Metrics tab**: hallucinations caught by type, tokens per question, router state, and benchmark results.

```
META/                       (the repository root)
└── meta-cv/
    ├── backend/            FastAPI app (graph store, verification engine, LLM client, benchmark, tests)
    ├── frontend/           React + Vite chat UI and metrics dashboard
    └── demo_server.py      backend with a scripted stand-in for the LLM (no API key needed)
```

All commands below are run from the `meta-cv` folder.

## Requirements

Python 3.12+ and Node.js 20+.

## Backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then fill in .env (see below)
python -m app.seed              # optional: loads a small starter graph
uvicorn app.main:app --port 8000 --reload
```

Check it is up: <http://localhost:8000/health> and the API docs at <http://localhost:8000/docs>.

### Configuration (`backend/.env`)

`.env` holds secrets and is gitignored, so it never comes with a clone. The backend also reads
`~/.meta_cv/.env` (outside the repo): keep a copy there and every clone on that machine works
without re-entering values. `backend/.env` wins where both set a key; set `META_ENV_FILE` to use
another file.

| Setting | Meaning |
|---|---|
| `GRAPH_BACKEND` | `sqlite` (no setup, good for trying it out) or `neo4j` (AuraDB) |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` | From the credentials file Aura gives you when you create an instance |
| `META_API_KEY`, `META_BASE_URL`, `META_MODEL` | Meta API (OpenAI-compatible) |
| `RULE_AUTO_THRESHOLD` | Leave empty so every LLM-proposed rule needs your approval |
| `MIN_TRIPLE_CONFIDENCE` | Extracted facts below this go to the review queue |

## Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the URL Vite prints (usually <http://localhost:5173>). Requests to `/api/...` are proxied to the backend on port 8000, so start the backend first.

## Try it without any API key

`demo_server.py` runs the real pipeline on a seeded SQLite graph with a scripted stand-in for the model. Run it with the backend's virtualenv, then start the frontend as above.

```powershell
backend\.venv\Scripts\python demo_server.py
```

## Tests and benchmark

```powershell
cd backend
python -m pytest
python -m app.bench --limit 5   # quick check: real LLM calls
python -m app.bench --train     # all 56 questions + routing-accuracy curve; fills the Metrics tab
```

The benchmark compares a plain LLM with this system on 56 graded questions across the three
risk types. Each answer is *correct*, *abstained* (declined or said it could not verify) or
*hallucinated* (answered wrongly). The factual questions assume the seed graph is loaded.

## How a question flows

```
question (+ conversation)
  -> exact-repeat cache (re-validated before reuse)                    engine/cache.py
  -> classify risk: factual | reasoning | consistency                   engine/router.py
  -> learned router picks the tier with the best track record           engine/router.py
  -> similar verified state injected                                    engine/states.py
  -> tier: factual (graph) | reasoning | consistency                    engine/factual.py, reasoning.py, memory.py
  -> conflict checks: single-valued relations, graph incompatible_with /
     contradicts edges, earlier turns of the conversation
  -> answer + metadata: risk type, tier, hallucinations caught, tokens, router scores
  -> turn logged; the outcome trains the router                         engine/pipeline.py
```

| Piece | Where | Notes |
|---|---|---|
| Hallucination taxonomy | `engine/router.py` `classify` | LLM classifier with a keyword fallback |
| Learned routing | `engine/router.py` `choose` | per (risk type, tier) success rate; a prior favours the specialist; small token penalty |
| Intermediate compression | `engine/states.py` | verified triples / checked steps, zlib-compressed JSON, lexical similarity |
| Conversation consistency | `engine/memory.py` | per-turn commitments, follow-up answering, standalone rewrite |
| Graph validation | `neo4j_store.py` (AuraDB), `graph_store.py` (SQLite) | functional + transitive rules, `incompatible_with` / `contradicts` edges |
| Metrics | `metrics.py`, `GET /metrics`, frontend Metrics tab | live turns + latest benchmark |
| Benchmark | `bench.py` | 56 graded questions; baseline vs system; `--train` adds the routing-accuracy curve |

The dashboard shows measured numbers only.

## Main endpoints

| Endpoint | Purpose |
|---|---|
| `POST /chat/stream` | Ask a question (with `conversation_id`); streams the thinking steps, then the answer |
| `POST /chat` | Same, returned as one response |
| `POST /feedback` | Mark an answer right or wrong; the router learns from it |
| `GET /metrics` | Dashboard numbers: live turns, router state, latest benchmark |
| `POST /ingest` | Extract facts from text into the graph (needs the Meta API) |
| `POST /validate` | Check one claim: supported, contradicted or unknown |
| `GET /rules/pending`, `POST /rules/{relation}/approve` | Review rules the LLM proposed |
| `GET /review`, `POST /review/{id}/accept` | Review facts that were held back |
