"""Export the AuraDB fact graph to a read-only SQLite file the app falls back to when Aura is unreachable.

    python -m app.export_snapshot [--out ../data/graph_snapshot.db]

Read-only against Aura. Copies facts (with provenance), relation rules and aliases. The app uses the
file automatically (GRAPH_SNAPSHOT) and reports degraded="graph_snapshot" while it does.
"""
import argparse
import os
import tempfile
from pathlib import Path

from . import config
from .graph_store import SqliteGraphStore


def export(neo, out: str) -> dict:
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=out_path.parent)
    os.close(fd)
    os.unlink(tmp)
    snap = SqliteGraphStore(tmp)
    for name, flags in neo._load_rules().items():
        snap.define_relation(name, **flags)
    batch, n = [], 0
    for f in neo.iter_facts():
        batch.append(f)
        if len(batch) >= 5000:
            n += snap.add_facts(batch)
            batch = []
    n += snap.add_facts(batch) if batch else 0
    aliases = neo._load_aliases()
    for a, c in aliases.items():
        snap.add_alias(a, c)
    os.replace(tmp, out_path)
    return {"facts": n, "relations": len(neo._load_rules()), "aliases": len(aliases), "path": str(out_path)}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Export the Aura graph to a read-only SQLite snapshot.")
    ap.add_argument("--out", default=config.GRAPH_SNAPSHOT)
    args = ap.parse_args(argv)
    from .neo4j_store import Neo4jGraphStore

    print(export(Neo4jGraphStore(), args.out))


if __name__ == "__main__":
    main()
