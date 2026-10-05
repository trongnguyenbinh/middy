"""Post-hoc analysis of one run: how much do ASR and the LLM slow each other down when they share the GPU?
Reads events.jsonl / llm_events.jsonl / samples.jsonl / slides.jsonl of run/<name>. Prints numbers only.
"""
import json
import os
import sys

import numpy as np

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run", sys.argv[1])
st = json.load(open(os.path.join(RUN, "stats.json"))) if os.path.exists(os.path.join(RUN, "stats.json")) else {"window_s": [0.0, None], "speed": 1.0, "audio_s": None}
start, speed = st["window_s"][0], st["speed"]
ev = [json.loads(l) for l in open(os.path.join(RUN, "events.jsonl"))]
le = [json.loads(l) for l in open(os.path.join(RUN, "llm_events.jsonl"))] if os.path.exists(os.path.join(RUN, "llm_events.jsonl")) else []
sm = [json.loads(l) for l in open(os.path.join(RUN, "samples.jsonl"))]
pct = lambda xs, q: round(float(np.percentile(xs, q)), 2) if len(xs) else None

# wall clock of audio time t: recover t0 from the first partial (t_emit - latency unknown) -> use the ready line instead:
# the orchestrator defines wall_of(t) = t0 + (t - start)/speed; t0 is not stored, so derive it from the minimum partial latency
# assumption: the fastest partial was emitted ~0.15 s after its audio arrived (measured pass-1 compute floor). Reported as "relative".
parts = [e for e in ev if e["type"] == "partial" and not e["final"]]
t0 = st.get("t0_wall") or (min(p["t_emit"] - (p["t_audio"] - start) / speed for p in parts) - 0.15 if parts else None)
wall_of = lambda t: t0 + (t - start) / speed

# LLM busy windows (generating and not paused)
busy = []
cur = None
for e in le:
    if e["event"] == "start":
        cur = [e["t"], None]
    elif e["event"] == "done" and cur:
        cur[1] = e["t"]; busy.append(tuple(cur)); cur = None
pauses = []
pcur = None
for e in le:
    if e["event"] == "pause":
        pcur = e["t"]
    elif e["event"] == "resume" and pcur is not None:
        pauses.append((pcur, e["t"])); pcur = None


def llm_active(t):
    return any(a <= t <= b for a, b in busy) and not any(a <= t <= b for a, b in pauses)


lat_busy = [p["t_emit"] - wall_of(p["t_audio"]) for p in parts if llm_active(p["t_emit"])]
lat_idle = [p["t_emit"] - wall_of(p["t_audio"]) for p in parts if not llm_active(p["t_emit"])]
finals = [e for e in ev if e["type"] == "final"]
c_busy = [f["compute_s"] for f in finals if llm_active(f["t_emit"])]
c_idle = [f["compute_s"] for f in finals if not llm_active(f["t_emit"])]
fl_busy = [f["t_emit"] - wall_of(f["speech_end"]) for f in finals if llm_active(f["t_emit"])]
fl_idle = [f["t_emit"] - wall_of(f["speech_end"]) for f in finals if not llm_active(f["t_emit"])]

# LLM speed while ASR is decoding: tokens between progress events vs. number of ASR emits in that interval
prog = [e for e in le if e["event"] in ("start", "progress", "done")]
emits = sorted(e["t_emit"] for e in ev if e["type"] in ("partial", "final"))
tps_asr, tps_quiet = [], []
for a, b in zip(prog, prog[1:]):
    if a.get("id") != b.get("id") or b["event"] == "start":
        continue
    dt = b["t"] - a["t"]; toks = (b.get("tokens", 0) if b["event"] == "progress" else b["stats"]["gen_tokens"]) - a.get("tokens", 0)
    if dt <= 0 or toks <= 0 or any(pa <= a["t"] <= pb or pa <= b["t"] <= pb for pa, pb in pauses):
        continue
    n_asr = sum(a["t"] <= t <= b["t"] for t in emits)
    (tps_asr if n_asr else tps_quiet).append(toks / dt)

# memory timeline per 5 min
mem = [s for s in sm if "swap_used_gb" in s]
tl = []
if mem:
    m0 = mem[0]["t"]
    for i in range(0, len(mem), max(1, len(mem) // 12)):
        s = mem[i]
        tl.append({"min": round((s["t"] - m0) / 60, 1), "swap_gb": s["swap_used_gb"], "mem_level": s.get("memorystatus_level"),
                   "rss": s["rss_gb"]})

out = {
    "run": sys.argv[1], "note": "t0 from stats.json" if st.get("t0_wall") else "t0 reconstructed from the fastest partial (-0.15 s); absolute values +-0.15 s, differences exact",
    "llm_busy_windows": len(busy), "llm_busy_total_s": round(sum(b - a for a, b in busy), 1), "llm_paused_total_s": round(sum(b - a for a, b in pauses), 1),
    "audio_s": st["audio_s"],
    "pass1_partial_latency_s": {"llm_busy": {"n": len(lat_busy), "p50": pct(lat_busy, 50), "p90": pct(lat_busy, 90)},
                                "llm_idle": {"n": len(lat_idle), "p50": pct(lat_idle, 50), "p90": pct(lat_idle, 90)}},
    "pass2_compute_s": {"llm_busy": {"n": len(c_busy), "p50": pct(c_busy, 50), "p90": pct(c_busy, 90)},
                        "llm_idle": {"n": len(c_idle), "p50": pct(c_idle, 50), "p90": pct(c_idle, 90)}},
    "final_latency_s": {"llm_busy": {"p50": pct(fl_busy, 50), "p90": pct(fl_busy, 90)}, "llm_idle": {"p50": pct(fl_idle, 50), "p90": pct(fl_idle, 90)}},
    "llm_tok_s": {"while_asr_emitting": {"n_intervals": len(tps_asr), "p50": pct(tps_asr, 50)}, "asr_quiet": {"n_intervals": len(tps_quiet), "p50": pct(tps_quiet, 50)}},
    "memory_timeline": tl,
    "slides": [{"t": s["t"], "latency_s": s["latency_s"], "n_words": len(s["words"]), "n_lines": s["n_lines"]} for s in map(json.loads, open(os.path.join(RUN, "slides.jsonl")))][:200] if os.path.exists(os.path.join(RUN, "slides.jsonl")) else None,
}
print(json.dumps(out, indent=1))
