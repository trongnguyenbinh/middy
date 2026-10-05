"""Replay a run's blocks.jsonl against the live-summary selection rules (rnd M1 v2 item 4). Numbers only.
  summary_replay.py <run-name>
Old rule: blocks with e > time cursor (cursor = e of the last block sent). New rule: per-block "summarised" mark.
Summaries fire at the real `start` times of the run's live generations, plus one end-of-meeting flush.
"""
import json
import os
import sys

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run", sys.argv[1])
fires = [json.loads(l)["t"] for l in open(os.path.join(RUN, "llm_events.jsonl")) if '"start"' in l and '"live' in l]
refined = {}                                                    # (source, s) -> (t_refined, e); ids renumber after a fallback merge
for l in open(os.path.join(RUN, "blocks.jsonl")):
    b = json.loads(l)
    if b["ev"] == "refined" and b["text"]:
        refined.setdefault((b["source"], b["s"]), (b["t_emit"], b["e"]))
cursor, sent_old, sent_new = 0.0, set(), set()
for t in fires + [float("inf")]:
    ready = [k for k, (tr, e) in refined.items() if tr <= t]
    old = [k for k in ready if refined[k][1] > cursor]
    if old:
        cursor = max(refined[k][1] for k in old); sent_old |= set(old)
    sent_new |= {k for k in ready if k not in sent_new}
lost = sorted(k for k in refined if k not in sent_old)
print(json.dumps({"run": sys.argv[1], "summaries": len(fires), "blocks_refined": len(refined),
                  "old_rule_sent": len(sent_old), "old_rule_lost": len(lost), "old_rule_lost_per_stream": {s: sum(k[0] == s for k in lost) for s in ("system", "mic")},
                  "new_rule_sent": len(sent_new), "new_rule_lost": len(refined) - len(sent_new)}))
