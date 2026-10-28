# META - adaptive chatbot with a hallucination taxonomy and learned routing

Every question is classified by the hallucination it is most at risk of, routed to the tier
built to prevent that type, checked, and logged so the router learns which tier actually works.

## Run it

```powershell
# backend (from backend/)
python -m venv ..\venv; ..\venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # fill in AuraDB + META_* values (see "Secrets" below)
python -m app.seed              # once: starter facts in the graph
uvicorn app.main:app --reload --port 8000

# frontend (from frontend/)
npm install
npm run dev                     # http://localhost:5173 - Chat and Metrics tabs

# tests / benchmark (from backend/)
python -m pytest
python -m app.bench --train     # real LLM calls; fills the Metrics dashboard
```

### Secrets

`backend/.env` is gitignored, so it never comes with a clone. The backend also reads
`~/.meta_cv/.env` (outside the repo), so keep a copy there and every clone on this machine
works without re-entering values. `backend/.env` wins where both set a key; `META_ENV_FILE`
points somewhere else.

## Pipeline

```
question (+ conversation)
  -> exact-repeat cache (re-validated before reuse)
  -> classify risk: factual | reasoning | consistency           engine/router.py
  -> learned router picks the tier with the best track record    engine/router.py
  -> similar verified state injected (intermediate compression)  engine/states.py
  -> tier
       factual      claims checked node by node against the graph,   engine/factual.py
                    then an independent judge + blind probe
       reasoning    arithmetic machine-checked, logic checked,         engine/reasoning.py
                    backtracking, independent solutions must agree
       consistency  answered from what the conversation already        engine/memory.py
                    verified, or rewritten as a standalone question
  -> conflict checks: single-valued relations, graph incompatible_with/contradicts
     edges, and earlier turns of the conversation
  -> answer + metadata (type, tier, hallucinations caught, tokens, router scores)
  -> turn logged; outcome = benchmark grade, else your Yes/No, else "fully verified"
```

| Piece | Where | Notes |
|---|---|---|
| Hallucination taxonomy | `engine/router.py` `classify` | LLM classifier with keyword fallback |
| Learned routing | `engine/router.py` `choose` | per (type, tier) success rate, prior favours the specialist, small token penalty |
| Intermediate compression | `engine/states.py` | verified triples / checked steps, zlib JSON, lexical similarity |
| Graph validation | `neo4j_store.py` (AuraDB), `graph_store.py` (SQLite) | functional + transitive rules, `incompatible_with` edges |
| Metrics | `metrics.py`, `GET /metrics`, frontend Metrics tab | live turns + latest benchmark |
| Benchmark | `bench.py` | 56 graded questions; baseline vs system; `--train` adds routing-accuracy curve |

Benchmark grading: *correct*, *abstained* (declined or said it could not verify) or
*hallucinated* (answered wrongly). The dashboard shows measured numbers only.
