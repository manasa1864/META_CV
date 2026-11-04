"""Tavily web search (tavily.com) as a second, independent search for the web evidence pool (websearch.py).

The model's built-in search (llm.web_search) finds pages and writes an answer with quotes. Tavily is a search API: it
returns pages with the text it extracted from them, and writes nothing itself. That gives the pool two things:
  - more pages: newer, niche or fan sources the model's search may not reach;
  - page text the app cannot fetch itself (pages that build their text with JavaScript), so a quote can still be
    confirmed word for word.
Its text is never taken as an answer. It is evidence that a claim's names must occur in, and that the independent judge must
find supports the claim, the same as any quote (websearch.check_claim).

Off unless TAVILY_API_KEY is set. The key is sent only to api.tavily.com, in the Authorization header, and is masked in
logs and reports (safety.mask_secrets). Search costs Tavily credits, not model tokens: one search per question.
"""
import re

import httpx

from .. import config, safety

API = "https://api.tavily.com/search"
MAX_SNIPPETS = 3          # sentences taken from each result as quotes
MAX_TEXT = 20_000         # characters of extracted page text kept per result

_http: httpx.Client | None = None


class Unavailable(Exception):
    pass


def enabled() -> bool:
    return bool(config.TAVILY_API_KEY)


def search(query: str) -> list[dict]:
    """[{url, title, content, text}]: content is Tavily's snippet, text the page text it extracted (may be empty).
    Raises Unavailable on any failure (no key, network, bad response)."""
    global _http
    if not enabled():
        raise Unavailable("no TAVILY_API_KEY")
    if _http is None:
        _http = httpx.Client(timeout=config.TAVILY_TIMEOUT)
    try:
        r = _http.post(API, headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
                       json={"query": query[:400], "search_depth": config.TAVILY_DEPTH, "max_results": config.TAVILY_MAX_RESULTS,
                             "include_raw_content": True, "include_answer": False})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise Unavailable(safety.mask_secrets(f"{type(e).__name__}: {e}")[:160]) from e
    out = []
    for x in data.get("results") or []:
        if isinstance(x, dict) and str(x.get("url", "")).startswith(("http://", "https://")):
            out.append({"url": x["url"], "title": str(x.get("title") or ""), "content": str(x.get("content") or ""),
                        "text": str(x.get("raw_content") or "")[:MAX_TEXT]})
    return out


def snippets(result: dict, min_len: int) -> list[str]:
    """Sentences from a result's snippet to use as quotes: Tavily's own extract of the page, never model output. Sentences
    with instruction-like text are dropped (a page can carry an injection too)."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(result["content"].split()))
    keep = [s.strip() for s in sentences if len(s.strip()) >= min_len and not safety.injection_signals(s)
            and not s.strip().endswith("...")]
    return keep[:MAX_SNIPPETS]
