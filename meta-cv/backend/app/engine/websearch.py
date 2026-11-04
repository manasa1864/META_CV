"""Evidence found on the web, so the knowledge graph does not have to hold everything.

When a claim is in neither the graph, Wikidata nor the cache, the model's own web search is used to find sources
(Responses API `web_search` tool, same key). The model can say anything about what it read, so its answer is not
trusted as it stands. Instead:
  1. one search per question: the model searches, reads pages and answers, quoting the sentences it relies on;
  2. this module fetches the cited pages ITSELF and keeps only the quotes that occur word for word on a page it
     fetched. Those verified quotes are the evidence pool; everything else the model said is ignored;
  3. a claim is checked against the pool: both its names must occur in the quotes, and an independent model call
     (the judge model, if configured) must find the quotes support it (or contradict it). Only then is it accepted
     (source "web", medium confidence) or rejected; otherwise the usual model checks decide;
  4. plots use the same pool, through the same event checks as Wikipedia (wikipedia.events_to_nodes).
Pages are fetched only from public addresses (no localhost, private or link-local ranges, also after redirects), with a
size and time limit. Quotes and answers have instruction-like sentences dropped. Off with WEB_SEARCH=0.

With TAVILY_API_KEY set, Tavily (tavily.py) searches too, at the same time. Its pages join the ones read here; a page the app
cannot read itself is matched against the text Tavily extracted from it; and sentences of Tavily's own snippets join the
pool as quotes (marked via "tavily"). It also keeps the pool working when the model's search is unavailable.
"""
import html
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import httpx

from .. import config, llm, meter, safety
from . import parallel, tavily
from .entities import normalize

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
USER_AGENT = "META-CV/1.0 (https://github.com/manasa1864/META_CV; evidence check)"
MIN_QUOTE = 25

RESEARCH_SYSTEM = """Find sources with web search and answer the question from them.
Rules:
- Quote the exact sentences you rely on, each in double quotes, copied word for word from the page, and put the
  page's address in brackets right after each quote. Never paraphrase inside quotes.
- If the sources do not say, say so. Do not fill gaps from memory.
- Begin your final answer with "FOUND:" if the sources answer the question, or "NOT FOUND:" if they do not.
- At most 400 words."""

EVIDENCE_SYSTEM = """You are checking a claim against quoted source text. Use only the quotes, never your own knowledge.
Say whether the quotes support the claim, contradict it, or do not address it.
Return only JSON: {"verdict": "supported" | "contradicted" | "not_addressed", "confidence": 0.0}"""

_http: httpx.Client | None = None
_URL = re.compile(r"https?://[^\s\]\)>\"”]+")
_QUOTE = re.compile(r"“([^”]{%d,500})”|\"([^\"\n]{%d,500})\"" % (MIN_QUOTE, MIN_QUOTE))


def enabled() -> bool:
    return config.WEB_SEARCH and (llm.configured() or tavily.enabled())


# ------------------------------------------------------------------------------------- fetching pages
def _public(url: str) -> bool:
    """http(s) to a public address only: no localhost, private, link-local or reserved ranges."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except (OSError, ValueError):
        return False


def _fetch(url: str) -> str | None:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=config.WEB_PAGE_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    for _ in range(MAX_REDIRECTS + 1):
        if not _public(url):
            return None
        try:
            with _http.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                kind = r.headers.get("content-type", "")
                if r.status_code != 200 or not any(t in kind for t in ("html", "text/plain", "xml")):
                    return None
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return body.decode(r.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, ValueError):
            return None
    return None


@lru_cache(maxsize=128)
def _page(url: str) -> str | None:
    """The page's text, normalised for matching (lowercase, no punctuation), or None if it cannot be read."""
    raw = _fetch(url)
    if raw is None:
        return None
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    return " " + normalize(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))) + " "


# ------------------------------------------------------------------------------------- the evidence pool
def _clean(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(s for s in sentences if not safety.injection_signals(s))


def _readable(text: str) -> str:
    """The model's answer for showing: no markdown marks, no inline addresses, no instruction-like sentences, and cut
    at a sentence end if it is long."""
    text = re.sub(r"\s*\[https?://[^\]\s]+\]", "", re.sub(r"[*#]+", "", text))
    text = _clean(" ".join(text.split())).strip()
    if len(text) > 1500:
        text = text[:1500].rsplit(". ", 1)[0] + "."
    return text


def _as_text(raw: str) -> str:
    return " " + normalize(raw) + " "


def verified_quotes(answer: str, urls: list[str], titles: dict[str, str],
                    extracted: dict[str, str] | None = None) -> tuple[list[dict], int]:
    """([{quote, url, title}], pages read): the quotes in the answer that occur word for word on a page fetched here, or in the
    text Tavily extracted from that page (extracted: url -> text; it reads pages that build their text with JavaScript)."""
    with ThreadPoolExecutor(max_workers=max(1, len(urls))) as ex:
        pages = dict(zip(urls, ex.map(_page, urls)))
    for u, raw in (extracted or {}).items():
        if raw.strip():
            pages[u] = (pages.get(u) or "") + _as_text(raw)
    read = {u: t for u, t in pages.items() if t and t.strip()}
    out, seen = [], set()
    for m in _QUOTE.finditer(answer):
        quote = " ".join((m.group(1) or m.group(2)).split())
        key = " " + normalize(quote) + " "
        if len(key) < MIN_QUOTE or key in seen or safety.injection_signals(quote):
            continue
        for url, text in read.items():
            if key in text:
                seen.add(key)
                out.append({"quote": quote, "url": url, "title": titles.get(url, "")})
                break
    return out, len(read)


def pool(question: str) -> dict | None:
    """This question's evidence pool, searched for once and kept on the question's meter. None when the search is
    off or failed. Keys: found (the sources answered it), answer (the model's cleaned answer), quotes (verified), text (the quotes joined), sources
    (pages it cited or found), read (pages fetched here), question."""
    m = meter.current()
    if m is not None and "web" in m:
        return m["web"]
    result = None
    if enabled():
        # the two searches are independent: run them together (parallel.submit keeps the question's meter counting)
        model_f = parallel.submit(_model_search, question) if llm.configured() or not tavily.enabled() else None
        tav_f = parallel.submit(_tavily_search, question) if tavily.enabled() else None
        r = model_f.result() if model_f else None
        tav = tav_f.result() if tav_f else []
        if (r and r["text"].strip()) or tav:
            r = r if r and r["text"].strip() else {"text": "", "citations": [], "results": []}
            titles = {x["url"]: x["title"] for x in r["results"] + r["citations"]} | {x["url"]: x["title"] for x in tav}
            # the cited pages, the pages the search returned, and the addresses the model wrote after its quotes;
            # then Tavily's pages
            urls = list(dict.fromkeys([c["url"] for c in r["citations"]] + _URL.findall(r["text"])
                                      + [x["url"] for x in r["results"]]))[:config.WEB_MAX_PAGES]
            urls = list(dict.fromkeys(urls + [x["url"] for x in tav]))
            extracted = {x["url"]: x["text"] for x in tav if x["text"]}
            quotes, read = verified_quotes(r["text"], urls, titles, extracted) if urls and r["text"] else \
                ([], sum(1 for x in tav if x["text"]))
            seen = {normalize(q["quote"]) for q in quotes}
            for x in tav:   # Tavily's snippets: page text it extracted, not anything a model wrote
                for s in tavily.snippets(x, MIN_QUOTE):
                    if normalize(s) not in seen:
                        seen.add(normalize(s))
                        quotes.append({"quote": s, "url": x["url"], "title": x["title"], "via": "tavily"})
            # the model says whether its sources answer the question; without the marker, assume they do. With only
            # Tavily, the pool is "found" when it has quotes, and there is no written answer to offer.
            found = not re.match(r"\W*NOT\s+FOUND", r["text"], re.I) if r["text"] else bool(quotes)
            result = {"question": question, "found": found, "answer": _readable(re.sub(r"^\W*(NOT\s+)?FOUND\s*:?", "", r["text"], flags=re.I)),
                      "quotes": quotes, "text": "\n".join(q["quote"] for q in quotes), "read": read,
                      "sources": [{"url": u, "title": titles.get(u, "")} for u in urls],
                      "tavily": len(tav)}
    if m is not None:
        m["web"] = result
    return result


def _model_search(question: str) -> dict | None:
    try:
        return llm.web_search(question, RESEARCH_SYSTEM)
    except (llm.LLMUnavailable, llm.LLMBadOutput):
        return None


def _tavily_search(question: str) -> list[dict]:
    try:
        return tavily.search(question)
    except tavily.Unavailable:
        return []


def cited(p: dict, limit: int = 3) -> list[dict]:
    """The pages that supplied verified quotes, most-quoted first."""
    count: dict[str, int] = {}
    for q in p["quotes"]:
        count[q["url"]] = count.get(q["url"], 0) + 1
    titles = {s["url"]: s["title"] for s in p["sources"]}
    return [{"url": u, "title": titles.get(u) or u} for u in sorted(count, key=lambda u: -count[u])[:limit]]


# ------------------------------------------------------------------------------------- checking a claim
def check_claim(c: dict, p: dict) -> dict:
    """{"verdict": supported | contradicted | not_addressed, "confidence", "pages"} for a claim against the pool."""
    text = " " + normalize(p["text"]) + " "
    if not p["quotes"] or not all(f" {normalize(x)} " in text for x in (c["subject"], c["object"]) if normalize(x)):
        return {"verdict": "not_addressed", "confidence": 0.0, "pages": []}
    try:
        d = llm.judge_json(EVIDENCE_SYSTEM, f"Quotes:\n{p['text']}\n\nClaim: {c['subject']} "
                                            f"{c['relation'].replace('_', ' ')} {c['object']}")
    except llm.LLMBadOutput:
        d = {}
    d = d if isinstance(d, dict) else {}
    verdict = str(d.get("verdict", "not_addressed")).lower()
    try:
        conf = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"verdict": verdict if verdict in ("supported", "contradicted") else "not_addressed", "confidence": conf,
            "pages": cited(p)}
