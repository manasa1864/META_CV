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
| `ADMIN_TOKEN` | long random string; write endpoints need header `X-Admin-Token`. Without it, writes work only from the machine the app runs on, so a deployment without a token is not writable by visitors |
| `TRUSTED_PROXY_HOPS` | proxies in front of the app that append to `X-Forwarded-For` (default `1`, right for a typical host). The rate limit reads the client IP that many entries from the right, because the left end is whatever the client sent. `0` if the app is exposed directly |
| `ALLOWED_ORIGINS` | other sites allowed to send POST/DELETE requests (default: the Vite dev server only). The app's own origin is always allowed |
| `DB_PATH` | path on a persistent volume if the host has one (default `/data/meta.db`) |
| `PORT` | the port the host expects (Hugging Face Spaces: `7860`) |
| `META_JUDGE_MODEL` | optional: a different model for the independent checks, so the drafter does not check itself (`META_JUDGE_BASE_URL` / `META_JUDGE_API_KEY` if it is on another API) |
| `WIKIDATA_LOOKUP` | optional: `0` turns off the Wikidata evidence lookup (on by default; needs outbound HTTPS to wikidata.org) |
| `WIKIPEDIA_LOOKUP` | optional: `0` turns off reading plots from Wikipedia for works the graph has no events for (on by default; needs outbound HTTPS to en.wikipedia.org) |
| `MAX_TOKENS`, `MAX_LLM_CALLS` | per-question budget (150000 tokens, 40 calls). A question that exceeds it stops with a clear message instead of guessing. A web search alone can use 13-25k tokens |
| `WEB_SEARCH` | optional: `0` turns off the model API's web search as evidence (on by default; ~10-17 s and ~10k tokens per question that needs it, plus the search fee; the app also fetches the cited pages, so it needs outbound HTTPS). `WEB_MAX_PAGES` (5), `WEB_PAGE_TIMEOUT` (8) |
| `PLOT_MEMORY_FALLBACK` | optional: `0` stops the model from telling what it remembers of a work's plot (labelled unverified, low confidence) when no source has it; such questions are declined instead |
| `META_REASONING_EFFORT` | `low` or `minimal` for Muse Spark (default model effort is high: ~30 s per call). Empty for providers that reject the parameter |
| `CHECK_WORKERS` | optional: model checks run at the same time (default `6`); lower it if the model API rate-limits, `1` checks one by one |
| optional | `RATE_LIMIT_PER_MIN` (12 in judge mode, 30 otherwise; `0` = off), `MAX_QUESTION_CHARS` (2000), `DAILY_TOKEN_BUDGET` (2000000), `MAX_CONCURRENT` (8), `LLM_TIMEOUT` (90) |
| `INVARIANCE_CHECK`, `ADVERSARIAL_ROUTING` | `1` (default) / `0`: re-answer flagged questions without their instructions; learned normal-vs-verified-only policy for flagged questions |
| `SOURCE_TRUST_MIN_FACTS`, `SOURCE_TRUST_FLOOR` | `3`, `0.5`: an untrusted source with at least this many checked facts and trust below the floor has its new facts sent to review |
| `TURNS_RETENTION_DAYS` | chat turns are kept this long, with private data already redacted (default `90`; `0` keeps them). Benchmark turns are kept |

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
