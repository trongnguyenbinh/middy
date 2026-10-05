"""Rolling speaker diarization per chunk (review M0 rnd-11): at the end of every notes chunk, run offline
diarization (sherpa-onnx, CPU) on that chunk only and map its local clusters onto global speakers by centroid
cosine. Labels from this step replace the online (order-dependent) labels of the chunk before the part notes.

05/10: the centroid of a local cluster is made from the CAM++ embeddings the ASR worker already computed for its VAD
segments (seg_end "emb"), not by a second CAM++ in the diarization process; pyannote + its own CAM++ (inside sherpa-onnx's
OfflineSpeakerDiarization, which cannot run without one) still run per chunk in diar_offline.py.

Known limit (rnd-v2 item 3): centroid matching across chunks is itself threshold-based; when the participant
count is known (num_speakers) no new global speaker is created beyond it.
"""
import os

import numpy as np
import sherpa_onnx

M = os.path.join(os.path.dirname(__file__), "..", "models")
SR = 16000


class ChunkDiarizer:
    def __init__(self, threshold=0.8, match_threshold=0.45, num_speakers=-1, threads=4):
        self.threshold, self.threads, self.diar = threshold, threads, None
        self.match_thr, self.num_speakers = match_threshold, num_speakers
        self.centroids = []      # global speakers: unit-norm running centroids
        self.seconds = []

    def _models(self):           # loaded on first use: the daemon only matches centroids, the models live in diar_offline.py (Lỗi 14)
        if self.diar is not None:
            return
        threshold, threads = self.threshold, self.threads
        self.cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=os.path.join(M, "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")), num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=os.path.join(M, "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold), min_duration_on=0.3, min_duration_off=0.5)
        self.diar = sherpa_onnx.OfflineSpeakerDiarization(self.cfg)

    def local_clusters(self, audio):
        """The heavy part (sherpa-onnx, holds the GIL): local clusters of the chunk, {cluster: [(s, e), ...]} in chunk time."""
        self._models()
        local = {}
        for r in self.diar.process(audio).sort_by_start_time():
            local.setdefault(r.speaker, []).append((r.start, r.end))
        return local

    @staticmethod
    def cluster_centroids(local, seg_embs, t_offset, min_share=0.6):
        """One unit centroid per local cluster from the ASR worker's segment embeddings [(s_abs, e_abs, emb)]: a segment counts
        (weighted by its length) for every cluster whose turns cover >= min_share of it; None when no segment does."""
        out = {}
        for k, turns in local.items():
            c = np.zeros(0)
            for s, e, emb in seg_embs:
                cov = sum(max(0.0, min(e, t_offset + b) - max(s, t_offset + a)) for a, b in turns)
                if e > s and cov >= min_share * (e - s):
                    u = np.asarray(emb, np.float32); u = u / (np.linalg.norm(u) + 1e-9) * (e - s)
                    c = u if not c.size else c + u
            out[k] = c / (np.linalg.norm(c) + 1e-9) if c.size else None
        return out

    def assign(self, local, cents, t_offset):
        """The light part (stateful): map local clusters onto the global speakers by centroid cosine."""
        # longest local clusters first so the dominant voices claim global ids before crumbs
        order = sorted(local, key=lambda k: -sum(e - s for s, e in local[k]))
        gmap = {}
        for k in order:
            c = cents[k]
            if c is None:
                gmap[k] = None; continue
            sims = [float(g @ c) for g in self.centroids]
            best = int(np.argmax(sims)) if sims else -1
            full = 0 < self.num_speakers <= len(self.centroids)
            if sims and (sims[best] >= self.match_thr or full):
                w = sum(e - s for s, e in local[k])
                self.centroids[best] = self.centroids[best] * self.seconds[best] + c * w
                self.centroids[best] /= np.linalg.norm(self.centroids[best]) + 1e-9
                self.seconds[best] += w
                gmap[k] = best
            else:
                self.centroids.append(c); self.seconds.append(sum(e - s for s, e in local[k])); gmap[k] = len(self.centroids) - 1
        out = [(t_offset + s, t_offset + e, f"Speaker {gmap[k] + 1}") for k in local if gmap[k] is not None for s, e in local[k]]
        return sorted(out), gmap

    @staticmethod
    def relabel(finals, dsegs):
        """Majority-overlap label for each final group; returns number of groups whose label changed."""
        changed = 0
        for g in finals:
            ov = {}
            for s0, e0, sp in dsegs:
                o = min(e0, g["e"]) - max(s0, g["s"])
                if o > 0:
                    ov[sp] = ov.get(sp, 0) + o
            if ov:
                new = max(ov, key=ov.get)
                changed += new != g.get("speaker")
                g["speaker_online"] = g.get("speaker_online", g.get("speaker"))
                g["speaker"] = new
        return changed
