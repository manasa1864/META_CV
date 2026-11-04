"""The two chatbot designs this system is compared with, on the same model and key.

plain  the question (and the conversation so far) goes straight to the model; its answer is returned unchanged.
rag    a typical retrieval-augmented chatbot: search Wikipedia for the question, put the top articles' text in the
       prompt, and let the model answer from it. No claim is checked, nothing is declined by rule, and there is no
       prompt-injection defence beyond what the model does by itself - which is what most RAG chatbots do.

Both call the model directly (not through llm._complete), so they do NOT get this system's data rule or the <data>
wrapping: they are the designs as usually built, not this system with parts switched off.
"""
import time

import openai

from . import config, llm
from .engine import wikipedia

PLAIN_SYSTEM = "You are a helpful assistant. Answer the user's question."
RAG_SYSTEM = ("You are a helpful assistant. Answer the user's question using the context passages below. "
              "If the context does not contain the answer, answer from your own knowledge.\n\nCONTEXT:\n{context}")
RAG_PASSAGES = 3
RAG_CHARS = 1500   # per passage: the article's lead, which is where most RAG chunkers' top chunk comes from


def _chat(system: str, history: list[tuple[str, str]], question: str) -> dict:
    """One model call with the conversation as real chat turns. Returns {answer, tokens, seconds}."""
    msgs = [{"role": "system", "content": system}]
    for q, a in history:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": question})
    t = time.time()
    last = None
    for attempt in range(3):
        try:
            resp = llm._client().chat.completions.create(
                model=config.META_MODEL, temperature=0, messages=msgs,
                **({"reasoning_effort": config.META_REASONING_EFFORT} if config.META_REASONING_EFFORT else {}))
            break
        except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError, openai.RateLimitError) as e:
            last = e
            time.sleep(4 * (attempt + 1))
    else:
        raise llm.LLMUnavailable(f"baseline call failed: {type(last).__name__}")
    u = getattr(resp, "usage", None)
    tokens = (u.prompt_tokens + u.completion_tokens) if u and u.prompt_tokens is not None else \
        llm.estimate_tokens(" ".join(m["content"] for m in msgs)) + llm.estimate_tokens(resp.choices[0].message.content or "")
    return {"answer": (resp.choices[0].message.content or "").strip(), "tokens": tokens, "seconds": round(time.time() - t, 1)}


def plain(question: str, history: list[tuple[str, str]] = ()) -> dict:
    return {**_chat(PLAIN_SYSTEM, list(history), question), "retrieved": []}


def retrieve(query: str) -> list[dict]:
    """Wikipedia full-text search, top RAG_PASSAGES articles, the first RAG_CHARS of each. Never raises."""
    try:
        hits = wikipedia._get(action="query", list="search", srsearch=query[:300], srlimit=RAG_PASSAGES)
        titles = [h["title"] for h in hits.get("query", {}).get("search", [])]
        if not titles:
            return []
        pages = wikipedia._get(action="query", prop="extracts", explaintext=1, exintro=1, redirects=1, titles="|".join(titles))
        by_title = {p.get("title"): p.get("extract", "") for p in pages.get("query", {}).get("pages", {}).values()}
        return [{"title": t, "text": (by_title.get(t) or "")[:RAG_CHARS]} for t in titles if by_title.get(t)]
    except Exception:  # noqa: BLE001 - a retrieval failure leaves the model with no context, as it would in a RAG app
        return []


def rag(question: str, history: list[tuple[str, str]] = ()) -> dict:
    # A follow-up ("where was she born?") is searched together with the previous question, as simple RAG apps do.
    query = f"{history[-1][0]} {question}" if history else question
    docs = retrieve(query)
    context = "\n\n".join(f"[{i}] {d['title']}\n{d['text']}" for i, d in enumerate(docs, 1)) or "(nothing found)"
    out = _chat(RAG_SYSTEM.format(context=context), list(history), question)
    return {**out, "retrieved": [d["title"] for d in docs]}
