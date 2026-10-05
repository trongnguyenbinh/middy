"""Public-file (B1) evaluations for M1 — numbers only.

  eval_b1.py cap  <wav16k>          VAD cap 25 s vs 12 s: pass-2 groups (>= 15 s / 2 s gap) -> Qwen offline (Vietnamese)
                                     -> term presence (thuat_ngu_loc.json of rnd, 44 terms) + the 2 answer windows of checkpoint 8
  eval_b1.py blocks <run-name>      sentence-final (pass 1) vs refined (pass 2) text of a finished run: term presence, answers
"""
import json
import os
import re
import sys
import unicodedata

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TERMS = json.load(open(os.environ.get("MIDY_TERMS_JSON", "terms.json")))["sap_terms"]
ANSWERS = {"AP_0100": (55, 85, r"purchasing a ?/? ?p\b"), "TYPE_0900": (535, 565, r"purchasing type")}
low = lambda s: re.sub(r"\s+", " ", unicodedata.normalize("NFC", s).lower())


def score(rows):
    """rows: [(s, e, text)] -> term presence count + answer hits inside the windows."""
    t = low(" ".join(r[2] for r in rows))
    out = {"terms_present": sum(re.search(rf"\b{re.escape(w)}\b", t) is not None for w in TERMS), "terms_total": len(TERMS), "chars": len(t)}
    for k, (a, b, rx) in ANSWERS.items():
        win = low(" ".join(r[2] for r in rows if r[1] > a and r[0] < b))
        out[k] = bool(re.search(rx, win))
    return out


if sys.argv[1] == "cap":
    import soundfile as sf
    from vad import StreamVad, WIN, SR
    from mlx_qwen3_asr import load_model, transcribe
    a, sr = sf.read(sys.argv[2], dtype="float32")
    model, _ = load_model("Qwen/Qwen3-ASR-1.7B")
    res = {}
    for cap in (25.0, 12.0):
        v = StreamVad(max_speech=cap); ev = []
        for i in range(0, len(a) // WIN * WIN, WIN):
            ev += v.feed(a[i:i + WIN])
        ev += v.flush()
        segs = []
        for k, t in ev:
            if k == "start":
                segs.append([t, None])
            else:
                segs[-1][1] = min(t + 0.2, len(a) / sr)
        # group like the worker: >= 15 s or a >= 2 s gap
        groups, cur = [], None
        for i, (s0, e0) in enumerate(segs):
            cur = [s0, e0] if cur is None else [cur[0], e0]
            gap = segs[i + 1][0] - e0 if i + 1 < len(segs) else 99
            if cur[1] - cur[0] >= 15 or gap >= 2:
                groups.append(cur); cur = None
        if cur:
            groups.append(cur)
        rows = []
        for s0, e0 in groups:
            r = transcribe(a[int(s0 * sr):int(e0 * sr)], model=model, language="Vietnamese")
            rows.append((s0, e0, r.text))
        res[f"cap_{int(cap)}s"] = {"segments": len(segs), "segments_at_cap": sum(e0 - s0 >= cap - 0.5 for s0, e0 in segs), "groups": len(groups),
                                  "seg_len_p50": round(float(np.median([e0 - s0 for s0, e0 in segs])), 1),
                                  "group_len_p50": round(float(np.median([e0 - s0 for s0, e0 in groups])), 1),
                                  "first_sentence_wait_p90_s": round(float(np.percentile([e0 - s0 for s0, e0 in groups], 90)), 1), **score(rows)}
        print(json.dumps({f"cap_{int(cap)}s": res[f"cap_{int(cap)}s"]}), flush=True)
    json.dump(res, open(os.path.join(HERE, "..", "run", "eval_b1_cap.json"), "w"), indent=1)

elif sys.argv[1] == "blocks":
    run = os.path.join(HERE, "..", "run", sys.argv[2])
    st = json.load(open(os.path.join(run, "stats.json"))) if os.path.exists(os.path.join(run, "stats.json")) else {}
    p1, p2 = {}, {}
    for l in open(os.path.join(run, "blocks.jsonl")):
        b = json.loads(l)
        (p1 if b["ev"] == "sentence_final" else p2)[b["id"]] = (b["s"], b["e"], b["text"])
    p1_rows = sorted(p1.values()); p2_rows = sorted(p2.values())
    out = {"run": sys.argv[2], "sentence_final": score(p1_rows), "refined": score(p2_rows), "blocks": st.get("blocks")}
    print(json.dumps(out, indent=1))
