"""Offline transcript of a recording with Midy's best pipeline (faster than real time, nothing played):
   ffmpeg 16 kHz mono -> language id on a few speech slices (labels only) -> Silero VAD -> groups >= 15 s / 2 s gap
   -> Qwen3-ASR 1.7B offline -> CJK filter -> sherpa-onnx offline diarization -> markdown with [hh:mm:ss] + Speaker N,
   lines merged per speaker and broken by the reference app's rule (>= 30 words and end punctuation). Output file mode 600.
Prints numbers only (blind: the content of a real meeting is never printed).
  transcribe_file.py <input media> <output .md> [--language English|Vietnamese|auto] [--num-speakers N]
Runs itself under the no-network sandbox (proto/nonet.sb) like the rest of Midy.
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("input"); ap.add_argument("output")
ap.add_argument("--language", default="auto")
ap.add_argument("--num-speakers", type=int, default=-1)
ap.add_argument("--diar-threshold", type=float, default=0.8)
ap.add_argument("--no-sandbox", action="store_true")
ap.add_argument("--relabel-from", default=None, help="existing transcript .md (or its .rows.json): redo ONLY diarization with the new speaker count")
A = ap.parse_args()
if not A.no_sandbox and os.environ.get("MIDY_SANDBOX") != "1":
    os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", os.path.join(HERE, "nonet.sb"), "-D", "RUN=" + os.path.realpath(os.path.join(HERE, "..", "run")),
                                        sys.executable] + sys.argv, dict(os.environ, MIDY_SANDBOX="1", HF_HOME=os.environ.get("MIDY_HF_HOME", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "hf")),
                                                                          HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
sys.path.insert(0, HERE)
from vad import StreamVad, WIN, SR  # noqa: E402
from glossary import language_drop, cjk_ratio  # noqa: E402
from blocks import Transcript  # noqa: E402

os.umask(0o077)
T0 = time.time()
timing = {}
tmp = tempfile.mkdtemp(prefix="midy_tf_")
wav = os.path.join(tmp, "audio.wav")
subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", A.input, "-vn", "-ac", "1", "-ar", str(SR), wav], check=True)
audio, sr = sf.read(wav, dtype="float32"); assert sr == SR
dur = len(audio) / SR
timing["decode_s"] = round(time.time() - T0, 1)

rows = None
if A.relabel_from:                         # reuse ASR text: rows.json sidecar if present, else parse the markdown lines
    import re
    side = A.relabel_from if A.relabel_from.endswith(".json") else A.relabel_from[:-3] + ".rows.json"
    if os.path.exists(side):
        rows = json.load(open(side))
    else:
        starts, rows = [], []
        for l in open(A.relabel_from):
            m = re.match(r"\*\*\[(\d\d):(\d\d):(\d\d)\] Speaker [^:]*:\*\* (.*)", l.rstrip("\n"))
            if m:
                rows.append({"s": int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]), "text": m[4], "dropped": False, "cjk": 0.0, "lang": "reused"})
        for a, b in zip(rows, rows[1:] + [None]):
            a["e"] = min(b["s"], a["s"] + 40) if b else min(dur, a["s"] + 40)
    segs, groups = [[r["s"], r["e"]] for r in rows], [[r["s"], r["e"]] for r in rows]
    lang, lang_counts = "reused", {}
    timing["reused_rows"] = len(rows)

# ---- VAD + groups (same rules as the live worker) --------------------------------------------------------------
t = time.time()
if rows is None:
    v = StreamVad(); ev = []
    for i in range(0, len(audio) // WIN * WIN, WIN):
        ev += v.feed(audio[i:i + WIN])
    ev += v.flush()
    segs = []
    for k, tt in ev:
        if k == "start":
            segs.append([tt, None])
        else:
            segs[-1][1] = min(tt + 0.2, dur)
    segs = [s for s in segs if s[1] is not None]
    groups, cur = [], None
    for i, (s0, e0) in enumerate(segs):
        cur = [s0, e0] if cur is None else [cur[0], e0]
        gap = segs[i + 1][0] - e0 if i + 1 < len(segs) else 99
        if cur[1] - cur[0] >= 15 or gap >= 2:
            groups.append(cur); cur = None
    if cur:
        groups.append(cur)
timing["vad_s"] = round(time.time() - t, 1)

# ---- language id on speech slices (labels only) ------------------------------------------------------------------
if rows is None:
    from mlx_qwen3_asr import load_model, transcribe  # noqa: E402
    model, _ = load_model("Qwen/Qwen3-ASR-1.7B")
    lang = A.language
    lang_counts = {}
if rows is None and lang == "auto":
    t = time.time()
    picks = [groups[int(len(groups) * f)] for f in (0.05, 0.15, 0.3, 0.5, 0.7, 0.85)] if len(groups) >= 6 else groups[:6]
    labels = []
    for s0, e0 in picks:
        r = transcribe(audio[int(s0 * SR):int(min(e0, s0 + 20) * SR)], model=model, language=None)
        labels.append(r.language)
    lang_counts = dict(collections.Counter(labels))
    top, n = collections.Counter(labels).most_common(1)[0]
    lang = top if n >= 0.67 * len(labels) else None       # mixed -> let Qwen detect per group
    timing["langid_s"] = round(time.time() - t, 1)

# ---- pass 2 per group --------------------------------------------------------------------------------------------
t = time.time()
if rows is None:
    rows = []
    for gi, (s0, e0) in enumerate(groups):
        r = transcribe(audio[int(s0 * SR):int(e0 * SR)], model=model, language=lang)
        text = r.text.strip()
        dropped = language_drop(text, lang or "English") if lang else False
        rows.append({"s": s0, "e": e0, "text": "" if dropped else text, "dropped": dropped, "cjk": round(cjk_ratio(text), 2), "lang": r.language})
    json.dump(rows, open(A.output[:-3] + ".rows.json", "w"), ensure_ascii=False)     # sidecar (600) for relabel-only reruns
    os.chmod(A.output[:-3] + ".rows.json", 0o600)
timing["asr_s"] = round(time.time() - t, 1)

# ---- offline diarization ------------------------------------------------------------------------------------------
t = time.time()
import sherpa_onnx  # noqa: E402
M = os.path.join(HERE, "..", "models")
cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=os.path.join(M, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")), num_threads=6),
    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=os.path.join(M, "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=6),
    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=A.num_speakers, threshold=A.diar_threshold), min_duration_on=0.3, min_duration_off=0.5)
diar = sherpa_onnx.OfflineSpeakerDiarization(cfg).process(audio).sort_by_start_time()
dsegs = [(d.start, d.end, d.speaker) for d in diar]
share = collections.Counter()
for s0, e0, sp in dsegs:
    share[sp] += e0 - s0
for g in rows:
    ov = {}
    for s0, e0, sp in dsegs:
        o = min(e0, g["e"]) - max(s0, g["s"])
        if o > 0:
            ov[sp] = ov.get(sp, 0) + o
    g["speaker"] = f"Speaker {max(ov, key=ov.get) + 1}" if ov else "Speaker ?"
timing["diar_s"] = round(time.time() - t, 1)

# ---- markdown (the reference app line rule) -----------------------------------------------------------------------------------
hms = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d}"
blocks = [{"source": "system", "speaker": g["speaker"], "text": g["text"], "s": g["s"], "dropped": g["dropped"]} for g in rows]
lines, cur = [], None
for b in blocks:
    if b["dropped"] or not b["text"]:
        continue
    if cur and cur["speaker"] == b["speaker"]:
        cur["text"] += " " + b["text"]
    else:
        if cur:
            lines.append(cur)
        cur = {"speaker": b["speaker"], "text": b["text"], "s": b["s"]}
    if len(cur["text"].split()) >= 30 and cur["text"].rstrip().endswith((".", "?", "!")):
        lines.append(cur); cur = None
if cur:
    lines.append(cur)
with open(A.output, "w") as f:
    f.write(f"# Transcript — {os.path.basename(A.input)}\n\n")
    f.write(f"Duration {hms(dur)} · language {lang or 'auto (mixed)'} · {len(rows)} segments · {len(share)} speakers · generated by Midy (local, offline)\n\n")
    for l in lines:
        f.write(f"**[{hms(l['s'])}] {l['speaker']}:** {l['text']}\n\n")
    n_drop = sum(g["dropped"] for g in rows)
    if n_drop:
        f.write(f"\n_{n_drop} segments in another language were dropped ({round(sum(g['e'] - g['s'] for g in rows if g['dropped']))} s)._\n")
os.chmod(A.output, 0o600)
timing["total_s"] = round(time.time() - T0, 1)
tot = sum(share.values()) or 1
print(json.dumps({"output": A.output, "mode": oct(os.stat(A.output).st_mode & 0o777), "audio_s": round(dur, 1), "speech_s": round(sum(e - s for s, e in segs), 1),
                  "vad_segments": len(segs), "groups": len(groups), "language": lang or "auto", "langid_labels": lang_counts,
                  "group_lang_labels": dict(collections.Counter(g["lang"] for g in rows)), "words": sum(len(g["text"].split()) for g in rows),
                  "groups_dropped": sum(g["dropped"] for g in rows), "cjk_ratio_max_kept": max([g["cjk"] for g in rows if not g["dropped"]] or [0]),
                  "speakers": len(share), "speaker_time_shares": [round(v / tot, 3) for _, v in share.most_common(8)], "lines": len(lines),
                  "timing": timing, "sandbox": os.environ.get("MIDY_SANDBOX") == "1"}))
