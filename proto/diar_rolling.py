"""Rolling speaker diarization per chunk (review M0 rnd-11): at the end of every notes chunk, run offline
diarization (sherpa-onnx, CPU) on that chunk only and map its local clusters onto global speakers by centroid
cosine. Labels from this step replace the online (order-dependent) labels of the chunk before the part notes.

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
        self.ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=os.path.join(M, "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=threads))

    def _embed(self, audio, segs, top=3):
        """Centroid of the `top` longest segments of one local cluster."""
        segs = sorted(segs, key=lambda x: x[1] - x[0], reverse=True)[:top]
        es = []
        for s0, e0 in segs:
            x = audio[int(s0 * SR):int(e0 * SR)]
            if len(x) < SR:
                continue
            st = self.ext.create_stream(); st.accept_waveform(SR, x); st.input_finished()
            e = np.array(self.ext.compute(st)); es.append(e / (np.linalg.norm(e) + 1e-9))
        if not es:
            return None
        c = np.mean(es, 0); return c / (np.linalg.norm(c) + 1e-9)

    def run(self, audio, t_offset):
        """audio: float32 16 kHz of one chunk; returns [(s_abs, e_abs, 'Speaker N')] and the local->global map."""
        return self.assign(*self.local_clusters(audio), t_offset)

    def local_clusters(self, audio):
        """The heavy part (sherpa-onnx, holds the GIL): local clusters of the chunk and one centroid embedding per cluster."""
        self._models()
        local = {}
        for r in self.diar.process(audio).sort_by_start_time():
            local.setdefault(r.speaker, []).append((r.start, r.end))
        return local, {k: self._embed(audio, local[k]) for k in local}

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
