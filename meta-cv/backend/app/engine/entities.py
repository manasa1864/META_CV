"""Entity names: one normal form for storage and lookup, plus conservative alias handling.

normalize() is applied by both graph stores on every write and lookup, so "J. K. Rowling",
"j.k. rowling" and "J K Rowling" meet at the same node. Anything riskier than formatting
(honorifics, initials) only *proposes* an alias; an alias is used once it is recorded
(store.add_alias), and an ambiguous one goes to the review queue instead of merging.
"""
import re
import unicodedata

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")      # keep decimal points
_PUNCT = re.compile(r"[\"(),;:!?]")
HONORIFICS = ("mr", "mrs", "ms", "miss", "dr", "sir", "lady", "lord", "prof", "professor", "saint", "st")


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name)).translate(_QUOTES).lower()
    s = _PUNCT.sub(" ", _DOT.sub(" ", s))
    return " ".join(s.split()).strip(" '-")


def alias_candidates(name: str, known: list[str]) -> list[str]:
    """Known names `name` could be a shortened form of ("j austen" -> "jane austen", "mr darcy" ->
    "fitzwilliam darcy"). Several candidates means the alias is ambiguous."""
    n = normalize(name).split()
    if not n:
        return []
    if n[0] in HONORIFICS and len(n) > 1:
        n = n[1:]
    out = []
    for k in known:
        parts = normalize(k).split()
        if not parts or parts == n or len(parts) < len(n):
            continue
        if parts[-1] != n[-1]:
            continue
        head = n[:-1]
        if all(len(h) == 1 for h in head) and len(head) <= len(parts) - 1 \
                and all(p.startswith(h) for h, p in zip(head, parts)):
            out.append(normalize(k))
    return sorted(set(out))
