"""Entity types and relation signatures: a cheap, deterministic check that rejects a claim whose subject or
object is the wrong kind of thing ("jane austen born_in 1775", "pride and prejudice written_by warsaw")
before any model call, and keeps such facts out of the graph at the write gate.

SIGNATURES says what each common relation connects (born_in: person -> place, birth_year: person -> year).
An entity's type comes from the graph itself, never from the model:
  - an explicit fact (x, entity_type, person) wins;
  - otherwise it is inferred from the signatures of the facts it already takes part in: the subject of
    born_in is a person, the object of located_in is a place. Mixed evidence means "unknown".
A claim is rejected only on clear evidence:
  - a year/number slot gets something with no year or number in it ("born_in year: steventon");
  - an entity slot gets an entity the graph types differently ("written_by warsaw", warsaw being a place);
  - a person or place slot gets a bare year or number ("born_in 1775").
An unknown type passes, and so does a relation without a signature. Titles that look like numbers ("1984")
are fine in work and organization slots. Add relations to SIGNATURES as the graph grows.
"""
import re
from collections import Counter

from .entities import normalize

PERSON, PLACE, WORK, ORG, YEAR, NUMBER, ANY, TYPE = (
    "person", "place", "work", "organization", "year", "number", "any", "type")
ENTITY_TYPES = (PERSON, PLACE, WORK, ORG)
TYPE_RELATION = "entity_type"

SIGNATURES: dict[str, tuple[str, str]] = {
    "born_in": (PERSON, PLACE), "died_in": (PERSON, PLACE), "lives_in": (PERSON, PLACE),
    "birth_year": (PERSON, YEAR), "death_year": (PERSON, YEAR), "nobel_prize_year": (PERSON, YEAR),
    "written_by": (WORK, PERSON), "directed_by": (WORK, PERSON), "published_in": (WORK, YEAR),
    "character_in": (PERSON, WORK),
    "located_in": (PLACE, PLACE), "capital_of": (PLACE, PLACE),
    "ceo_of": (PERSON, ORG), "founded_by": (ORG, PERSON), "headquartered_in": (ORG, PLACE),
    "marries": (PERSON, PERSON), "married_to": (PERSON, PERSON), "spouse_of": (PERSON, PERSON),
    "parent_of": (PERSON, PERSON), "child_of": (PERSON, PERSON),
    TYPE_RELATION: (ANY, TYPE),
}

_YEAR = re.compile(r"^\d{1,4}(?: ?(?:bc|bce|ad|ce))?$")
_NUMBER = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_HAS_YEAR = re.compile(r"(?<!\d)\d{3,4}(?!\d)")


def literal_type(name: str) -> str | None:
    n = normalize(name)
    if _YEAR.match(n):
        return YEAR
    return NUMBER if _NUMBER.match(n) else None


def known_type(store, name: str) -> str | None:
    """The entity's type as the graph shows it, or None when the graph is silent or mixed."""
    n = store.resolve(name) if hasattr(store, "resolve") else normalize(name)
    votes: Counter = Counter()
    for f in store.facts_about(n, limit=200):
        if f["relation"] == TYPE_RELATION and f["subject"] == n and f["object"] in ENTITY_TYPES:
            return f["object"]
        sig = SIGNATURES.get(f["relation"])
        if not sig:
            continue
        if f["subject"] == n and sig[0] in ENTITY_TYPES:
            votes[sig[0]] += 1
        if f["object"] == n and sig[1] in ENTITY_TYPES:
            votes[sig[1]] += 1
    return next(iter(votes)) if len(votes) == 1 else None


def _mismatch(store, name: str, want: str) -> str | None:
    """Why `name` cannot fill a `want` slot, or None if it can (or we cannot tell)."""
    if want == ANY:
        return None
    if want == TYPE:
        return None if normalize(name) in ENTITY_TYPES else f"is not one of: {', '.join(ENTITY_TYPES)}"
    lit = literal_type(name)
    if want == YEAR:
        if lit == YEAR or _HAS_YEAR.search(normalize(name)):
            return None
        kt = known_type(store, name)
        return f"is a {kt}" if kt else "has no year in it"
    if want == NUMBER:
        if lit:
            return None
        kt = known_type(store, name)
        return f"is a {kt}" if kt else "is not a number"
    kt = known_type(store, name)
    if kt and kt != want:
        return f"is a {kt} in the knowledge graph"
    if not kt and lit and want in (PERSON, PLACE):
        return f"is a {lit}"
    return None


def check(store, subject: str, relation: str, obj: str) -> str | None:
    """None if the claim's subject and object fit the relation's signature; otherwise why not."""
    sig = SIGNATURES.get(str(relation).lower())
    if not sig:
        return None
    for name, want, role in ((subject, sig[0], "subject"), (obj, sig[1], "object")):
        why = _mismatch(store, name, want)
        if why:
            article = "an" if want[0] in "aeiou" else "a"
            return f"{relation} needs {article} {want} as its {role}, but '{normalize(name)}' {why}"
    return None
