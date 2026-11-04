"""Outside evidence for plot questions the knowledge graph cannot answer, from Wikipedia.

A plot has no independent claim to check: the blind probe cannot reproduce "Shen Qiao's defeat" because the
model just made that label up. So the model is not asked to remember a plot. Instead:
  1. find the work's Wikipedia article (the title or opening must name the work) and read its plot section;
  2. the model lists the story's events from that text, in order;
  3. every event is checked against the article: its names and numbers must occur in the text (lexical),
     and a second, independent model call must find each event supported by it (judge model if configured);
  4. events that pass become answer nodes with source "wikipedia", marked medium confidence. They are never
     cached or reused: the article can change, and the model's reading of it is not a graph fact.
Anything else - lookup off, no article, no plot, Wikipedia unreachable, too few events verified - returns no nodes
with a reason, and the caller declines with that reason (the model never drafts a plot from memory).
Sentences in the article that look like instructions are dropped before the model sees the text. Off with
WIKIPEDIA_LOOKUP=0. Only the work's name goes to Wikipedia; responses are cached in the process.
"""
import re
from functools import lru_cache

import httpx

from .. import config, llm, safety
from . import guardrails
from .entities import normalize
from .trace import Trace

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "PrismAI/1.0 (https://github.com/manasa1864/META_CV; plot evidence)"
MAX_TEXT = 6000
MAX_EVENTS = 12
MIN_EVENTS = 2
PLOT_HEADINGS = ("plot", "plot summary", "synopsis", "summary", "premise", "story", "storyline", "overview",
                 "plot overview", "background and premise")

EVENTS_SYSTEM = """List the main events of the story told in the text, in the order the text tells them.
Each event is one short sentence that uses only names and facts that appear in the text. "involves" lists the
named characters who take part. At most 12 events. Do not add anything the text does not say.
If the text is not an account of this work's story (for example it is about the author, or only mentions the work),
return an empty list: {"events": []}.
Return only JSON: {"events": [{"event": "...", "involves": ["..."]}]}"""

CHECK_SYSTEM = """You are checking someone else's list of events against a text; do not assume it is right.
For each numbered event say whether the text supports it (what happens, to whom, and its place in the order).
Return only JSON: {"results": [{"n": 1, "supported": true}]}"""

_http: httpx.Client | None = None


class _Unreachable(Exception):
    pass


def enabled() -> bool:
    return config.WIKIPEDIA_LOOKUP


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


def _clean(text: str) -> str:
    """Drop sentences that look like instructions to a model; the rest of the article is data."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(s for s in sentences if not safety.injection_signals(s))


def plot_text(extract: str) -> tuple[str, str] | None:
    """(text, section name) - the article's plot section, else its opening - or None if there is too little."""
    pieces = re.split(r"(?m)^==(?!=)\s*(.+?)\s*==\s*$", extract)   # [opening, title, body, title, body, ...]
    lead, sections = pieces[0], list(zip(pieces[1::2], pieces[2::2]))
    for name in PLOT_HEADINGS:
        for title, body in sections:
            if title.strip().lower() == name:
                body = re.sub(r"(?m)^=+.*?=+\s*$", "", body).strip()   # drop sub-headings, keep their text
                if len(body) >= 200:
                    return body[:MAX_TEXT], title.strip()
    return (lead.strip()[:MAX_TEXT], "opening") if len(lead.strip()) >= 300 else None


_STORY = re.compile(r"\b(novel|novella|series|film|movie|book|play|poem|epic|saga|manga|anime|television|tv|"
                    r"opera|musical|short story|fairy tale|legend|myth|webtoon|light novel|video game|fable)\b", re.I)


def _bare(name: str) -> str:
    """A title as compared: no "(novel)"-style qualifier, no leading article, no case or punctuation."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    return re.sub(r"^(the|a|an) ", "", normalize(name))


def _fetch(title: str) -> tuple[dict, set[str]] | None:
    """(page, the names that redirect to it) for a title, or None if there is no such page."""
    data = _get(action="query", prop="extracts", explaintext=1, exsectionformat="wiki", titles=title, redirects=1)
    q = data.get("query", {})
    page = next(iter(q.get("pages", {}).values()), {})
    if not page or "missing" in page or "invalid" in page:
        return None
    return page, {_bare(r.get("from", "")) for r in q.get("redirects", [])}


def _about(wanted: str, page: dict, redirected_from: set[str]) -> str | None:
    """How this article is about the work: "title" (its title IS the work's name; a longer title is a different work -
    "The Thousand Autumns of Jacob de Zoet" is not "Thousand Autumns"), "redirect" (the work's name redirects to it),
    or None. Disambiguation pages are never the work."""
    title, extract = page.get("title", ""), page.get("extract") or ""
    if "disambiguation" in title.lower() or "may refer to" in extract[:400].lower():
        return None
    if _bare(title) == wanted:
        return "title"
    return "redirect" if wanted in redirected_from else None


@lru_cache(maxsize=256)
def _article(work: str) -> tuple[str, str, str, str] | None:
    """(title, url, text, section) of the article about this work, or None. Raises _Unreachable."""
    wanted = _bare(work)
    hits = _get(action="query", list="search", srsearch=work, srlimit=3).get("query", {}).get("search", [])
    # the exact name first (it follows Wikipedia's own redirects), then what the search found
    for title in dict.fromkeys([work.title(), *(h["title"] for h in hits)]):
        got = _fetch(title)
        how = _about(wanted, *got) if got else None
        if not how:
            continue
        page = got[0]
        extract = page.get("extract") or ""
        found = plot_text(extract)
        # An article with no plot section is used only if its title is the work's name and its opening says it is a
        # story ("Dune" the sand formation is not Dune the novel). A redirect needs a real plot section: a work's
        # name often redirects to its author's page ("Manacled" -> "SenLinYu"), and a biography is not its plot.
        if found and (found[1] != "opening" or (how == "title" and _STORY.search(extract[:500]))):
            title = page.get("title", title)
            return title, "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"), _clean(found[0]), found[1]
    return None


def _events(work: str, text: str) -> list[dict]:
    data = llm.chat_json(EVENTS_SYSTEM, f"Work: {work}\n\nText:\n{text}")
    raw = data.get("events") if isinstance(data, dict) else None
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and str(e.get("event", "")).strip():
            who = [str(w).strip() for w in e.get("involves") or [] if str(w).strip()]
            out.append({"event": " ".join(str(e["event"]).split()).rstrip("."), "involves": who})
        if len(out) >= MAX_EVENTS:
            break
    return out


def _lexical(events: list[dict], text: str) -> list[dict]:
    """Events whose names and numbers all occur in the text. A sentence's first word is not checked as a name
    (it is capitalised anyway); the rest are."""
    allowed = " " + normalize(text) + " "
    return [e for e in events if not guardrails.unsupported_terms(e["event"][:1].lower() + e["event"][1:], allowed)
            and all(f" {normalize(w)} " in allowed for w in e["involves"])]


def _supported(events: list[dict], text: str) -> list[dict]:
    """Events a second, independent model call finds supported by the text."""
    listing = "\n".join(f"{i}. {e['event']}" for i, e in enumerate(events, 1))
    try:
        data = llm.judge_json(CHECK_SYSTEM, f"Text:\n{text}\n\nEvents:\n{listing}")
    except llm.LLMBadOutput:
        return []
    rows = data.get("results") if isinstance(data, dict) else None
    ok = {r.get("n") for r in rows if isinstance(r, dict) and r.get("supported") is True} if isinstance(rows, list) else set()
    return [e for i, e in enumerate(events, 1) if i in ok]


def events_to_nodes(work: str, text: str, k: int, source: str, where: str, **extra) -> tuple[list[dict], int, int]:
    """(nodes, events checked, events listed): the story's events read from `text` and held to it, in story order.
    Fewer than MIN_EVENTS verified means no nodes. `where` names the evidence for the node's reason."""
    events = _events(work, text)
    named = _lexical(events, text)
    checked = _supported(named, text) if named else []
    if len(checked) < MIN_EVENTS:
        return [], len(checked), len(events)
    nodes = [{"subject": e["event"].lower(), "relation": "event_in", "object": work, "status": "accepted",
              "source": source, "depends_on": [], "part": k, "order": i, "reason": f"supported by {where}", **extra,
              "detail": f"event {i} of {len(checked)}" + (f"; involves {', '.join(e['involves'])}" if e["involves"] else "")}
             for i, e in enumerate(checked, 1)]
    return nodes, len(checked), len(events)


def plot_nodes(part: dict, k: int, trace: Trace):
    """Generator: answer a plot part - (?, event_in, work) - from Wikipedia. Returns (nodes, reason): the verified
    events in story order, or ([], why not)."""
    work = part["object"]
    ev = trace.start("factual", f"Looking for a plot of “{work}” on Wikipedia",
                     "the graph holds no events for it, and a plot cannot be checked from the model's memory")
    yield ev

    def fail(reason: str, level: str = "warn"):
        return trace.settle(ev, level, reason)

    try:
        article = _article(work)
    except _Unreachable as e:
        reason = f"Wikipedia could not be reached ({str(e)[:60]})"
        yield fail(reason)
        return [], reason
    if not article:
        reason = f"no Wikipedia article about “{work}” with a plot section was found"
        yield fail(reason)
        return [], reason
    title, url, text, section = article
    nodes, n_ok, n_all = events_to_nodes(work, text, k, "wikipedia", f"the Wikipedia article “{title}” ({section} section)",
                                         title=title, url=url)
    if not nodes:
        reason = (f"the summary of Wikipedia's “{title}” could not be checked against the article "
                  f"({n_ok} of {n_all} events held up)")
        yield fail(reason)
        return [], reason
    yield trace.settle(ev, "ok", f"Read the {section} section of Wikipedia's “{title}”: {n_ok} of {n_all} "
                                 "events passed the name check and an independent check against the article text. "
                                 "Medium confidence: this is the model's reading of the article, not a stored fact.")
    return nodes, None
