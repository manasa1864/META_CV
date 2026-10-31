"""Outside evidence from Wikidata for claims the knowledge graph does not hold.

The model judge and blind probe are a model checking a model. For relations Wikidata records, this looks the
claim up instead: find the subject's item, read the mapped property, and compare its values with the claim.
  supported     a value matches exactly (a label or alias, ignoring case, punctuation and "the"; or the year
                of a date). Loose matches do not count: "york" must not pass for "new york".
  contradicted  the property is single-valued in reality (birth / death place and date), the top-ranked item
                with exactly the subject's name has values, and none match - the value comes back with it
  unknown       anything else: relation not mapped, no unambiguous item, no values, or Wikidata unreachable
It never raises: a failure is "unknown", and the model checks decide as before. Off with WIKIDATA_LOOKUP=0.
Only the claim's names and the property go to Wikidata; responses are cached in the process.
"""
import re
from functools import lru_cache

import httpx

from .. import config
from .entities import normalize

API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "META-CV/1.0 (https://github.com/manasa1864/META_CV; hallucination checks)"

# relation -> (properties, value kind, exclusive). Exclusive: one true value in reality, so a mismatch is a
# contradiction. ceo_of is read from the organization's side (P169 "chief executive officer").
PROPERTIES: dict[str, tuple[tuple[str, ...], str, bool]] = {
    "born_in": (("P19",), "item", True),
    "died_in": (("P20",), "item", True),
    "birth_year": (("P569",), "year", True),
    "death_year": (("P570",), "year", True),
    "written_by": (("P50",), "item", False),
    "directed_by": (("P57",), "item", False),
    "published_in": (("P577",), "year", False),
    "character_in": (("P1441",), "item", False),
    "located_in": (("P131", "P17", "P30"), "item", False),
    "capital_of": (("P1376",), "item", False),
    "headquartered_in": (("P159",), "item", False),
    "founded_by": (("P112",), "item", False),
    "marries": (("P26",), "item", False),
    "married_to": (("P26",), "item", False),
    "spouse_of": (("P26",), "item", False),
    "child_of": (("P22", "P25"), "item", False),
    "parent_of": (("P40",), "item", False),
    "won": (("P166",), "item", False),
    "genre": (("P136",), "item", False),
}
REVERSED = {"ceo_of": ("P169", "item")}  # (person, ceo_of, org): look up the org's P169 for the person

_http: httpx.Client | None = None
_YEAR = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")


class _Unreachable(Exception):
    pass


def enabled() -> bool:
    return config.WIKIDATA_LOOKUP


def covers(relation: str) -> bool:
    return relation in PROPERTIES or relation in REVERSED


def _get(**params) -> dict:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=config.WIKIDATA_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        r = _http.get(API, params={**params, "format": "json"})
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise _Unreachable(str(e)) from e


@lru_cache(maxsize=512)
def _search(name: str) -> tuple[tuple[str, str], ...]:
    """(qid, label) of items whose label or alias matches the name exactly (normalised), best first."""
    data = _get(action="wbsearchentities", search=name, language="en", type="item", limit=7)
    out = []
    for hit in data.get("search", []):
        texts = {normalize(hit.get("label", "")), normalize((hit.get("match") or {}).get("text", ""))}
        if normalize(name) in texts:
            out.append((hit["id"], hit.get("label", "")))
    return tuple(out)


@lru_cache(maxsize=512)
def _claims(qid: str) -> dict:
    data = _get(action="wbgetentities", ids=qid, props="claims", languages="en")
    return (data.get("entities", {}).get(qid) or {}).get("claims", {})


_name_cache: dict[str, tuple[str, ...]] = {}


def _names(qids: list[str]) -> dict[str, tuple[str, ...]]:
    """English label and aliases of each item: one batched request for those not cached yet."""
    todo = [q for q in dict.fromkeys(qids) if q not in _name_cache]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        data = _get(action="wbgetentities", ids="|".join(chunk), props="labels|aliases", languages="en")
        for q in chunk:
            ent = data.get("entities", {}).get(q) or {}
            names = [(ent.get("labels", {}).get("en") or {}).get("value", "")]
            names += [a.get("value", "") for a in ent.get("aliases", {}).get("en", [])]
            _name_cache[q] = tuple(n for n in names if n)
    return {q: _name_cache.get(q, ()) for q in qids}


def _values(qid: str, props: tuple[str, ...], kind: str) -> list[tuple[str, tuple[str, ...]]]:
    """[(display value, names to compare)] for the item's statements of these properties."""
    claims = _claims(qid)
    raw = [((st.get("mainsnak") or {}).get("datavalue") or {}).get("value") for p in props for st in claims.get(p, [])]
    if kind == "year":
        years = [str(int(m.group(1))) for v in raw if isinstance(v, dict) and v.get("time")
                 for m in [_YEAR.search(v["time"])] if m]
        return [(y, (y,)) for y in years]
    ids = [v["id"] for v in raw if isinstance(v, dict) and v.get("id")]
    names = _names(ids)
    return [(names[q][0], names[q]) for q in ids if names[q]]


def _key(name: str) -> str:
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]+", " ", normalize(name)).split() if w not in ("the", "a", "an"))


def _match(kind: str, claimed: str, names: tuple[str, ...]) -> bool:
    if kind == "year":
        m = _YEAR.search(normalize(claimed))
        return bool(m) and str(int(m.group(1))) in names
    return _key(claimed) in {_key(n) for n in names}


def _unknown(reason: str) -> dict:
    return {"verdict": "unknown", "reason": reason}


def check(subject: str, relation: str, obj: str) -> dict:
    """Look the claim up on Wikidata. Never raises."""
    if not enabled() or not covers(relation):
        return _unknown("Wikidata lookup not used for this relation")
    try:
        if relation in REVERSED:
            prop, kind = REVERSED[relation]
            holder, wanted, props, exclusive = obj, subject, (prop,), False
        else:
            props, kind, exclusive = PROPERTIES[relation]
            holder, wanted = subject, obj
        items = _search(holder)
        if not items:
            return _unknown(f"no Wikidata item named '{holder}'")
        for qid, label in items[:2]:
            values = _values(qid, props, kind)
            hit = next((v for v, names in values if _match(kind, wanted, names)), None)
            if hit:
                return {"verdict": "supported", "qid": qid, "value": hit,
                        "reason": f"Wikidata {qid} ({label}) gives {hit}"}
        qid, label = items[0]  # Wikidata ranks the best-known item with this exact name first
        values = _values(qid, props, kind)
        if exclusive and values:
            shown = sorted({v for v, _ in values})
            return {"verdict": "contradicted", "qid": qid, "values": shown,
                    "reason": f"Wikidata {qid} ({label}) gives {', '.join(shown)}, not {normalize(wanted)}"}
        return _unknown(f"Wikidata {qid} does not confirm it" if values else f"Wikidata {qid} has no value for it")
    except _Unreachable as e:
        return _unknown(f"Wikidata unreachable ({str(e)[:80]})")
