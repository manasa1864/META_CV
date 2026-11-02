"""Plots as ordered events, so "what happens in this book" can be verified step by step like any other fact.

A plot is a list of events. Each event is an entity whose name is a short description, linked by:
  (event, event_in, work)        the event belongs to the work
  (event, event_order, "n")      its place in the story (single-valued)
  (event, involves, character)   who takes part (any number)
  (event, follows, earlier)      it comes after the previous event (transitive and acyclic, so
                                 "X follows Y" is checked as a path through the whole story, and a claim
                                 that reverses the order contradicts it - see factual.verify_node)
Plot questions are answered graph-first (decompose.py): the lookup (?, event_in, work) returns every
stored event, which is then put in story order and told in that order. Events are written through the
write gate like any fact (add_plot), by the seed or POST /plots.
"""
EVENT_IN, ORDER, INVOLVES, FOLLOWS = "event_in", "event_order", "involves", "follows"
RELATIONS = [  # name, functional, transitive, acyclic
    (EVENT_IN, False, False, False),
    (ORDER, True, False, False),
    (INVOLVES, False, False, False),
    (FOLLOWS, False, True, True),
]


def define(store) -> None:
    for name, functional, transitive, acyclic in RELATIONS:
        store.define_relation(name, functional, transitive, acyclic)


def add_plot(store, work: str, events: list[dict], source: str | None = None, *, added_by: str | None = None,
             batch_id: str | None = None) -> dict:
    """Write a work's events in story order. events: [{"event": "short description", "involves": [names]}]."""
    from .. import graph_guard  # noqa: PLC0415  (the write gate imports the engine's type checks)

    define(store)
    counts = {"accepted": 0, "new": 0, "quarantined": []}
    prev = None
    for i, ev in enumerate(events, 1):
        name = ev["event"]
        rows = [(name, EVENT_IN, work), (name, ORDER, str(i))] + [(name, INVOLVES, p) for p in ev.get("involves") or []]
        if prev:
            rows.append((name, FOLLOWS, prev))
        for s, r, o in rows:
            res = graph_guard.add_fact_checked(store, s, r, o, source, added_by=added_by, batch_id=batch_id)
            if res["status"] == "accepted":
                counts["accepted"] += 1
                counts["new"] += bool(res.get("created"))
            else:
                counts["quarantined"].append({"fact": f"{s} | {r} | {o}", "reason": res["reason"], "status": res["status"]})
        prev = name
    return {"work": work, "events": len(events), **counts}


def details(store, events: list[str]) -> dict[str, dict]:
    """{event: {"order": n or None, "involves": [...]}} straight from the graph."""
    out = {}
    for e in events:
        facts = [f for f in store.facts_about(e, limit=200) if f["subject"] == e]
        orders = [f["object"] for f in facts if f["relation"] == ORDER]
        out[e] = {"order": int(orders[0]) if orders and orders[0].isdigit() else None,
                  "involves": sorted(f["object"] for f in facts if f["relation"] == INVOLVES)}
    return out


def in_story_order(store, nodes: list[dict]) -> list[dict]:
    """Event nodes (subject = event) sorted into story order, each with a detail note for the answer."""
    info = details(store, [n["subject"] for n in nodes])
    total = sum(1 for d in info.values() if d["order"] is not None)
    out = []
    for n in nodes:
        d = info[n["subject"]]
        note = (f"event {d['order']} of {total}" if d["order"] is not None else "event, order unknown") \
            + (f"; involves {', '.join(d['involves'])}" if d["involves"] else "")
        out.append({**n, "order": d["order"], "detail": note})
    return sorted(out, key=lambda n: (n["order"] is None, n["order"] or 0, n["subject"]))
