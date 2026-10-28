# META CV

A chatbot that checks its answers against a knowledge graph before it says them, and shows its thinking live while it works.

- **Factual questions** are checked claim by claim against the graph, so a wrong fact stops the chain instead of spreading.
- **Reasoning and maths questions** are re-checked for contradictions at three strictness levels (conservative, balanced, aggressive).
- **Cache** reuses reasoning paths that were already verified.
- The UI streams each step ("See the thinking"), says when processing is complete, then gives the answer.

```
META/                       (the repository root)
└── meta-cv/
    ├── backend/            FastAPI app (graph store, verification engine, LLM client, tests)
    ├── frontend/           React + Vite chat UI
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
python -m app.seed              # loads a small starter graph
uvicorn app.main:app --port 8000 --reload
```

Check it is up: <http://localhost:8000/health> and the API docs at <http://localhost:8000/docs>.

### Configuration (`backend/.env`)

`.env` holds secrets and is not in the repo. Copy it from `.env.example`.

| Setting | Meaning |
|---|---|
| `GRAPH_BACKEND` | `sqlite` (no setup, good for trying it out), `neo4j` (Aura) or `falkordb` |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` | From the credentials file Aura gives you when you create an instance |
| `META_API_KEY`, `META_BASE_URL`, `META_MODEL` | Meta API (OpenAI-compatible) |
| `RULE_AUTO_THRESHOLD` | Leave empty so every LLM-proposed rule needs your approval |
| `MIN_TRIPLE_CONFIDENCE` | Extracted facts below this go to the review queue |

To run without Aura, set `GRAPH_BACKEND=sqlite`.

## Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open the URL Vite prints (usually <http://localhost:5173>). Requests to `/api/...` are proxied to the backend on port 8000, so start the backend first.

## Try it without any API key

`demo_server.py` runs the real pipeline on a seeded SQLite graph with a scripted stand-in for the model. Run it from the repo root with the backend's virtualenv, then start the frontend as above.

```powershell
backend\.venv\Scripts\python demo_server.py
```

## Tests

```powershell
cd backend
python -m pytest
```

## Main endpoints

| Endpoint | Purpose |
|---|---|
| `POST /chat/stream` | Ask a question; streams the thinking steps, then the answer |
| `POST /chat` | Same, returned as one response |
| `POST /ingest` | Extract facts from text into the graph (needs the Meta API) |
| `POST /validate` | Check one claim: supported, contradicted or unknown |
| `GET /rules/pending`, `POST /rules/{relation}/approve` | Review rules the LLM proposed |
| `GET /review`, `POST /review/{id}/accept` | Review facts that were held back |
