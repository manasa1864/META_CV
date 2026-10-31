"""Record real runs of the example questions for Replay mode.

    python -m app.record_replays [--only false-premise]

Runs each example through the live pipeline (real model, real graph) and saves the full event stream
with its timing to replays/<id>.json. This is the only way replay files are made: the UI labels them
"Recorded run" and never presents them as live. Without a configured model it refuses to run.
"""
import argparse
import json
import time
import uuid

from . import config, llm
from .db import init_db
from .engine import pipeline
from .examples import EXAMPLES
from .graph_store import get_store


def record(store, example: dict) -> dict:
    conv = f"replay-{uuid.uuid4().hex[:8]}"
    turns = []
    for q in example["turns"]:
        if example.get("separate"):
            conv = f"replay-{uuid.uuid4().hex[:8]}"
        t0, events = time.perf_counter(), []
        for ev in pipeline.run(store, q, conversation_id=conv, source="replay", typing_delay=0):
            events.append({"t": round(time.perf_counter() - t0, 3), "event": ev})
        if any(e["event"]["type"] == "error" for e in events):
            raise RuntimeError(f"{example['id']}: the live run failed ({events[-1]['event'].get('code')}); nothing saved")
        turns.append({"question": q, "events": events})
    return {"id": example["id"], "label": example["label"], "question": example["turns"][0],
            "recorded_at": time.time(), "model": config.META_MODEL, "backend": store.backend, "turns": turns}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Record real example runs for Replay mode.")
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    if not llm.configured():
        raise SystemExit("The model is not configured: replays are recorded from real runs only.")
    init_db()
    store = get_store()
    config.REPLAYS_DIR.mkdir(parents=True, exist_ok=True)
    for ex in EXAMPLES:
        if args.only and ex["id"] != args.only:
            continue
        data = record(store, ex)
        (config.REPLAYS_DIR / f"{ex['id']}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"recorded {ex['id']}: {sum(len(t['events']) for t in data['turns'])} events")


if __name__ == "__main__":
    main()
