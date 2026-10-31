# Deploying META CV for judges

One container serves the built frontend at `/` and the API under `/api` (the same paths the Vite dev
proxy uses), built by the multi-stage `Dockerfile`:

```bash
docker build -t meta-cv .
docker run -p 8000:8000 --env-file backend/.env -e JUDGE_MODE=1 -e ADMIN_TOKEN=... meta-cv
```

No host-specific files are in the repo yet. Pick a host from the table below first.

## Environment variables

All configuration comes from environment variables; the frontend never receives a secret, and the
container sets `META_NO_HOME_ENV=1` so nothing reads `~/.meta_cv/.env`.

| Variable | Value |
|---|---|
| `GRAPH_BACKEND` | `neo4j` |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | from the Aura credentials file |
| `META_API_KEY`, `META_BASE_URL`, `META_MODEL` | the model |
| `JUDGE_MODE` | `1` - read-only graph, frozen router, per-IP rate limit, daily token budget |
| `ADMIN_TOKEN` | long random string; write endpoints need header `X-Admin-Token` |
| `DB_PATH` | path on a persistent volume if the host has one (default `/data/meta.db`) |
| `PORT` | the port the host expects (Hugging Face Spaces: `7860`) |
| optional | `RATE_LIMIT_PER_MIN` (12), `DAILY_TOKEN_BUDGET` (2000000), `MAX_CONCURRENT` (8), `LLM_TIMEOUT` (30) |

## Where state lives

- **Fact graph:** AuraDB.
- **Everything else** (turns, router outcomes, caches, review queue, quarantine, rule proposals): SQLite at `DB_PATH`.
  - **With a persistent volume:** mount it and point `DB_PATH` at it.
  - **Without one:** the app starts from `data/initial_state.db` if that file is shipped, and every restart resets learning. Keep `JUDGE_MODE=1` (frozen router) so this is visible and harmless. Ship `data/initial_state.db` only from a real run: run the benchmark against a fresh `DB_PATH`, then copy that file.
- **Benchmark results** (`results/`) and **recorded runs** (`replays/`) are committed files, so the results page and Replay mode work with no model and no graph.

## Hosts (check current plans before choosing; they change)

| Host | Builds this Dockerfile | Persistent disk | Notes |
|---|---|---|---|
| Render | yes | paid instances only | free instances sleep after inactivity (slow first request) |
| Fly.io | yes | volumes | needs a card; machines can auto-stop/start |
| Railway | yes | volumes | usage-based billing after the trial |
| Hugging Face Spaces (Docker) | yes | paid add-on | set `PORT=7860`; free Spaces sleep when idle |

## AuraDB Free: pausing and limits

- **Pausing:** an idle free instance pauses after a few days, and can be deleted after a long pause.
  - The app cannot resume it: resume it in the Aura console.
  - Keep it awake with an external scheduler (for example a cron job or uptime monitor) calling `GET https://<host>/api/readyz` every 12 hours.
- **If Aura is unreachable anyway:** the app answers read-only from `data/graph_snapshot.db`, and `/readyz` and the UI say "running on a local snapshot".
  - Refresh the snapshot before deploying: `cd backend && python -m app.export_snapshot`.
- **Plan limits:** the free tier caps nodes and relationships. Read the exact numbers in the console.
  - A write that hits the limit fails with `graph_limit` and is never retried.
  - `python -m app.scale` measures where the free tier stops being suitable.

## Before the demo

```bash
cd backend
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
