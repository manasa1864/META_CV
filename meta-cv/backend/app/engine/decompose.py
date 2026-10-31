"""Graph-first answering: split a factual question into the parts it asks for, and answer each part
straight from the knowledge graph before any model drafting.

  plan()  one model call: the question as triples with one unknown "?" (a lookup) or none (a yes/no check).
          "#n" stands for the answer of part n ("where was the author born" -> ("#1", born_in, ?)).
  fill()  each part from the graph alone. A lookup returns EVERY stored value, so a graph-backed answer
          cannot quietly leave some out. A yes/no part is validated; a contradiction comes back as the
          stored value (graph-corrected), like a corrected claim.
  gaps()  after the model drafted the remaining parts, which parts still have no verified answer. Each
          becomes an "unverified" node, so the answer says what it could not verify and is never "high".

Parts the graph cannot answer go to the usual draft-and-verify walk (factual.py), which is told exactly
which parts are still needed. Off per question with the no_graph_first flag (benchmark ablation).
"""
import re

from .. import llm, meter
from .trace import Trace

MAX_PARTS = 6
LOOKUP_LIMIT = 500
UNKNOWN = "?"
_REF = re.compile(r"^#(\d+)$")
_FILLER = {"of", "in", "by", "to", "the", "a", "an", "at", "on", "is", "has", "was", "for"}

PLAN_SYSTEM = """Split the question into the facts it asks for, as knowledge-graph lookups.
Each part is one subject-relation-object triple with exactly one unknown "?" (a lookup) or no unknown (a yes/no check).
- subject / object: an entity name as written in the question, "?" for the unknown, or "#n" for the answer of an
  earlier part n (1-based) when a part builds on it ("where was the author born" -> subject "#1").
- relation: snake_case. Reuse a relation from the list whenever one fits, in its stored direction
  (book written_by person, character character_in book, person born_in place).
- Only the parts the question actually asks for, at most 6. A question that asks for no facts has no parts.
Return only JSON: {"parts": [{"subject": "...", "relation": "...", "object": "?"}]}"""


def enabled() -> bool:
    m = meter.current()
    return not (m and m.get("flags", {}).get("no_graph_first"))


def _side(value, pos: dict[int, int]) -> str | None:
    v = " ".join(str(value or "").lower().split())
    if v in ("?", "unknown"):
        return UNKNOWN
    m = _REF.match(v)
    if m:
        k = pos.get(int(m.group(1)) - 1)  # the model's numbering -> our list, refs to dropped parts fail
        return f"#{k + 1}" if k is not None else None
    return v or None


def parse(data) -> list[dict]:
    raw = data.get("parts") if isinstance(data, dict) else None
    out: list[dict] = []
    pos: dict[int, int] = {}
    for i, p in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(p, dict):
            continue
        s, o = _side(p.get("subject"), pos), _side(p.get("object"), pos)
        rel = llm.normalize_relation(p.get("relation", ""))
        if not (s and o and rel) or (s == UNKNOWN and o == UNKNOWN):
            continue
        pos[i] = len(out)
        out.append({"subject": s, "relation": rel, "object": o})
        if len(out) >= MAX_PARTS:
            break
    return out


def _expand(side: str, values: dict[int, list[str]]) -> list[str] | None:
    """A side's concrete names: itself, the answers of the part it refers to, or None if those are unknown."""
    m = _REF.match(side)
    if not m:
        return [side]
    return values.get(int(m.group(1)) - 1) or None


def _show(side: str, values: dict[int, list[str]] | None) -> str:
    m = _REF.match(side)
    if not m:
        return side
    got = (values or {}).get(int(m.group(1)) - 1)
    return " / ".join(got) if got else f"[answer to part {m.group(1)}]"


def describe(p: dict, values: dict[int, list[str]] | None = None) -> str:
    return f"{_show(p['subject'], values)} | {p['relation']} | {_show(p['object'], values)}"


def plan(store, question: str, entities: list[str], trace: Trace):
    """Generator: the question's parts, or [] when it cannot be split (the whole question is then drafted)."""
    ev = trace.start("graph", "Splitting the question into the facts it asks for")
    yield ev
    try:
        data = llm.chat_json(PLAN_SYSTEM, f"Relations in the graph: {store.relation_names()}\n"
                                          f"Entities mentioned: {entities}\n\nQuestion: {question}")
    except llm.LLMBadOutput:
        data = None
    parts = parse(data)
    if parts:
        yield trace.settle(ev, "ok", f"{len(parts)} part(s): " + "; ".join(
            f"{k}. {describe(p)}" for k, p in enumerate(parts, 1)))
    else:
        yield trace.settle(ev, "info", "No separate parts found - the whole answer will be drafted and verified.")
    return parts


def _node(s, r, o, k, source="graph", reason=None, **extra) -> dict:
    return {"subject": s, "relation": r, "object": o, "status": "accepted", "source": source,
            "reason": reason or f"answered from the graph (part {k + 1})", "depends_on": [], "part": k, **extra}


def _lookup(store, known: str, relation: str, unknown_is_object: bool, k: int) -> list[dict]:
    name = store.resolve(known) if hasattr(store, "resolve") else known
    side = "subject" if unknown_is_object else "object"
    return [_node(f["subject"], relation, f["object"], k) for f in store.facts_about(name, limit=LOOKUP_LIMIT)
            if f["relation"] == relation and f[side] == name]


def _check(store, s: str, relation: str, o: str, k: int) -> list[dict]:
    try:
        v = store.validate(s, relation, o)
    except ValueError:
        return []
    if v["verdict"] == "supported":
        how = " via " + " -> ".join(v["path"]) if v.get("path") else ""
        return [_node(s, relation, o, k, reason=f"{v['reason']}{how} (part {k + 1})")]
    if v["verdict"] == "contradicted":
        from .factual import _stored_values  # noqa: PLC0415  (factual imports this module)

        return [_node(s, relation, stored, k, source="graph-corrected", reason=v["reason"], original_object=o)
                for stored in _stored_values(store, s, relation)]
    return []


def fill(store, parts: list[dict], trace: Trace):
    """Generator: answer each part from the graph alone. Returns (answered nodes, open part indexes)."""
    answered: list[dict] = []
    values: dict[int, list[str]] = {}
    open_parts: list[int] = []
    for k, p in enumerate(parts):
        ev = trace.start("graph", f"Part {k + 1}/{len(parts)}: {describe(p, values)}", "looking it up in the graph")
        yield ev
        subjects, objects = _expand(p["subject"], values), _expand(p["object"], values)
        if subjects is None or objects is None:
            open_parts.append(k)
            yield trace.settle(ev, "info", "It builds on a part the graph could not answer - the model drafts it, "
                                           "and every claim is verified.")
            continue
        nodes: list[dict] = []
        if UNKNOWN in (p["subject"], p["object"]):
            unknown_is_object = p["object"] == UNKNOWN
            for known in (subjects if unknown_is_object else objects):
                nodes += _lookup(store, known, p["relation"], unknown_is_object, k)
            values[k] = sorted({n["object"] if unknown_is_object else n["subject"] for n in nodes})
        else:
            for s in subjects:
                for o in objects:
                    nodes += _check(store, s, p["relation"], o, k)
            values[k] = sorted({n["object"] for n in nodes})
        if not nodes:
            open_parts.append(k)
            yield trace.settle(ev, "info", "Not in the graph - the model drafts it, and every claim is verified.")
            continue
        answered += nodes
        corrected = [n for n in nodes if n["source"] == "graph-corrected"]
        if corrected:
            yield trace.settle(ev, "warn", f"The graph says otherwise: {', '.join(n['object'] for n in corrected)}, "
                                           f"not {corrected[0]['original_object']} ({corrected[0]['reason']}).")
        else:
            yield trace.settle(ev, "ok", f"From the graph: {', '.join(values[k])}"
                                         + (" (every stored value)" if len(values[k]) > 1 else "") + ".")
    return answered, open_parts


def focus_text(parts: list[dict], open_parts: list[int], answered: list[dict]) -> str:
    """What the drafter is told: what the graph already answered, and the exact parts still needed."""
    values = _values(parts, answered)
    done = "\n".join(f"- {n['subject']} | {n['relation']} | {n['object']}" for n in answered) or "(none)"
    need = "\n".join(f"- {describe(parts[k], values)}" for k in open_parts)
    return (f"Already answered from the knowledge graph (do not repeat these):\n{done}\n\n"
            f"Still needed - answer ONLY these parts, keeping these exact names and relation names:\n{need}")


def _rel_match(a: str, b: str) -> bool:
    if a == b:
        return True
    wa, wb = set(a.split("_")) - _FILLER, set(b.split("_")) - _FILLER
    return bool(wa & wb)


def _values(parts: list[dict], nodes: list[dict]) -> dict[int, list[str]]:
    """The answers each part has among the accepted nodes, in order (later parts may refer to earlier ones)."""
    from .factual import same_entity  # noqa: PLC0415

    accepted = [n for n in nodes if n.get("status") == "accepted"]
    values: dict[int, list[str]] = {}
    for k, p in enumerate(parts):
        subjects, objects = _expand(p["subject"], values), _expand(p["object"], values)
        hits = []
        for n in accepted:
            if not _rel_match(n["relation"], p["relation"]):
                continue
            # The known side must match (an unresolved reference matches anything). A yes/no part is
            # answered by any value for its subject: a corrected value answers it too.
            if p["subject"] == UNKNOWN:
                ok = objects is None or any(same_entity(n["object"], o) for o in objects)
            else:
                ok = subjects is None or any(same_entity(n["subject"], s) for s in subjects)
            if ok:
                hits.append(n)
        values[k] = sorted({n["subject"] if p["subject"] == UNKNOWN else n["object"] for n in hits})
    return values


def gaps(parts: list[dict], nodes: list[dict]) -> list[dict]:
    """An 'unverified' node for every part that no accepted node answers."""
    values = _values(parts, nodes)
    return [{"subject": _show(p["subject"], values), "relation": p["relation"], "object": _show(p["object"], values),
             "status": "unverified", "source": "coverage", "reason": "no verified answer for this part of the question",
             "depends_on": [], "part": k, "part_text": describe(p, values)}
            for k, p in enumerate(parts) if not values.get(k)]
