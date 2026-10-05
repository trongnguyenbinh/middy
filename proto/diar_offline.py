"""Whole-meeting offline diarization (design 1.5a step 2) in its OWN process (Lỗi 14): sherpa-onnx holds the Python GIL for the
whole run (measured 29/09: 300 s of audio = 8.3 s during which every other daemon thread was frozen). In the daemon that froze the
next meeting while the old one finished in the background.
  diar_offline.py whole <audio.npy float32 16 kHz> <num_speakers> <threshold>  -> stdout JSON [[start_s, end_s, speaker], ...]
  diar_offline.py chunk <audio.npy> <threshold>  -> {"local": {cluster: [[s, e], ...]}, "cents": {cluster: embedding | null}}
                  (the heavy half of diar_rolling.ChunkDiarizer.run; the daemon keeps the global centroids and calls assign())
"""
import json
import os
import sys

import numpy as np
import sherpa_onnx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
M = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")
x = np.load(sys.argv[2])
if sys.argv[1] == "chunk":
    from diar_rolling import ChunkDiarizer
    local, cents = ChunkDiarizer(threshold=float(sys.argv[3])).local_clusters(x)
    print(json.dumps({"local": local, "cents": {k: (c.tolist() if c is not None else None) for k, c in cents.items()}}))
    sys.exit()
cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
    segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
        pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=os.path.join(M, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")), num_threads=4),
    embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=os.path.join(M, "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=4),
    clustering=sherpa_onnx.FastClusteringConfig(num_clusters=int(sys.argv[3]), threshold=float(sys.argv[4])), min_duration_on=0.3, min_duration_off=0.5)
print(json.dumps([[d.start, d.end, d.speaker] for d in sherpa_onnx.OfflineSpeakerDiarization(cfg).process(x).sort_by_start_time()]))
