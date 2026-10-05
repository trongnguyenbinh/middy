"""Replay the transcript_window events of a run through a reference-style v2 client and compare what it would display
with the transcript's final state (rnd M1 item 1 acceptance). Numbers only.
  client_check.py <run-name>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blocks import WindowClient  # noqa: E402

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run", sys.argv[1])
c = WindowClient(); n = 0; settled = 0
for l in open(os.path.join(RUN, "window_events.jsonl")):
    ev = json.loads(l); c.apply(ev); n += 1; settled += "settled" in ev
client = c.text()
fp = os.path.join(RUN, "blocks_final.json")
if os.path.exists(fp):                      # server-side final state written by core._finish (labels included)
    truth = [(b["speaker"], b["text"]) for b in json.load(open(fp)) if b["text"]]
else:                                       # older runs: last block view per id from blocks.jsonl (labels may be stale)
    final = {}
    for l in open(os.path.join(RUN, "blocks.jsonl")):
        b = json.loads(l); final[b["id"]] = b
    truth = [(b["speaker"], b["text"]) for _, b in sorted(final.items()) if b["text"]]
# blocks.jsonl carries one line per event; after a fallback merge ids are renumbered, so compare on text only
ct, tt = [t for _, t in client], [t for _, t in truth]
print(json.dumps({"run": sys.argv[1], "events": n, "settled_events": settled, "client_warnings": c.warnings, "client_blocks": len(ct),
                  "match_texts": ct == tt, "match_speakers": client == truth,
                  "refined_seen_by_client": sum(1 for e in c.entries if e["state"] in ("refined", "frozen"))}))
