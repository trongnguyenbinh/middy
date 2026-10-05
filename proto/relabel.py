"""Re-run ONLY the online speaker-labelling step on the VAD segments of a finished run (CPU, no ASR).
Compares the old rule (compare with the first embedding of each speaker, sherpa SpeakerEmbeddingManager)
with the new rule (running centroid, speakers.py). Prints counts and time shares only.
"""
import json
import os
import sys

import numpy as np
import sherpa_onnx
import soundfile as sf

sys.path.insert(0, os.path.dirname(__file__))
from speakers import CentroidSpeakers  # noqa: E402

RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run", sys.argv[1])
thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.45
min_s = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
st = json.load(open(os.path.join(RUN, "stats.json")))
start = st["window_s"][0]
audio, sr = sf.read(os.path.join(RUN, "audio.wav"), dtype="float32")
segs = [json.loads(l) for l in open(os.path.join(RUN, "events.jsonl")) if '"seg_end"' in l]
segs = [g for g in segs if g["stream"] in ("spk", "system") and g["e"] - g["s"] >= min_s]
ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
    model=os.path.join(os.path.dirname(__file__), "..", "models", "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=4))
old = sherpa_onnx.SpeakerEmbeddingManager(ext.dim)
new = CentroidSpeakers(threshold=thr)
n_old = 0
share_old, share_new = {}, {}
for g in segs:
    x = audio[int((g["s"] - start) * sr):int((g["e"] - start) * sr)]
    s = ext.create_stream(); s.accept_waveform(sr, x); s.input_finished()
    e = np.array(ext.compute(s))
    name = old.search(e, threshold=thr)
    if not name:
        n_old += 1; name = f"Speaker {n_old}"; old.add(name, e)
    share_old[name] = share_old.get(name, 0) + g["e"] - g["s"]
    name2 = new.label(e)
    share_new[name2] = share_new.get(name2, 0) + g["e"] - g["s"]
tot = sum(share_old.values())
fmt = lambda d: [round(v / tot, 3) for _, v in sorted(d.items(), key=lambda kv: -kv[1])]
print(json.dumps({"run": sys.argv[1], "threshold": thr, "min_segment_s": min_s, "n_segments": len(segs), "labelled_seconds": round(tot, 1),
                  "old_rule_first_embedding": {"n_speakers": n_old, "time_shares": fmt(share_old)},
                  "new_rule_running_centroid": {"n_speakers": len(new), "time_shares": fmt(share_new)}}))
