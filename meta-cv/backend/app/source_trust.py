"""How often a source's facts check out, learned from the write gate.

Every fact a source submits is checked by the write gate (graph_guard). When the check has evidence, the
source's record changes: the graph or Wikidata already backs the fact (confirmed), or the graph or Wikidata
contradicts it, its types do not fit, or it carries instructions (contradicted). Trust is the Beta(1, 1) mean
(confirmed + 1) / (confirmed + contradicted + 2). Once an untrusted source (not in TRUSTED_SOURCES) has
SOURCE_TRUST_MIN_FACTS checked facts and its trust is below SOURCE_TRUST_FLOOR, its new facts go to the review
queue instead of the graph, even when nothing contradicts them: a source that keeps being wrong is the likely
origin of the wrong facts nothing can check yet. A reviewer's accept bypasses this.
"""
import time

from . import config
from .db import connect


def record(source: str | None, outcome: str) -> None:
    """outcome: 'confirmed' or 'contradicted'. Facts without a source are not tracked."""
    if not source or outcome not in ("confirmed", "contradicted"):
        return
    with connect() as conn:
        conn.execute(f"INSERT INTO source_trust(source, {outcome}, updated_at) VALUES (?, 1, ?) ON CONFLICT(source) "
                     f"DO UPDATE SET {outcome} = {outcome} + 1, updated_at = excluded.updated_at", (source, time.time()))


def _row(source: str) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT confirmed, contradicted FROM source_trust WHERE source=?", (source,)).fetchone()
    return {"confirmed": r["confirmed"], "contradicted": r["contradicted"]} if r else {"confirmed": 0, "contradicted": 0}


def trust(source: str | None) -> dict:
    """{"source", "trust", "checked", "confirmed", "contradicted", "trusted"} for one source."""
    r = _row(source or "")
    n = r["confirmed"] + r["contradicted"]
    return {"source": source, "trust": round((r["confirmed"] + 1) / (n + 2), 3), "checked": n, **r,
            "trusted": source in config.TRUSTED_SOURCES}


def held_back(source: str | None) -> str | None:
    """Why this source's new facts need review (None: they may go straight through the gate)."""
    if not source or source in config.TRUSTED_SOURCES:
        return None
    t = trust(source)
    if t["checked"] >= config.SOURCE_TRUST_MIN_FACTS and t["trust"] < config.SOURCE_TRUST_FLOOR:
        return (f"source '{source}' is often contradicted (trust {t['trust']:.2f}: {t['contradicted']} of "
                f"{t['checked']} checked facts were wrong)")
    return None


def table(limit: int = 20) -> list[dict]:
    """Sources with a record, least trusted first."""
    with connect() as conn:
        names = [r["source"] for r in conn.execute("SELECT source FROM source_trust")]
    return sorted((trust(s) for s in names), key=lambda t: (t["trust"], -t["checked"]))[:limit]
