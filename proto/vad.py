"""Streaming Silero VAD (silero_vad_v6.onnx, MIT, copied to models/; no faster-whisper dependency).

Same rules as faster_whisper.vad.get_speech_timestamps used for the offline measurements:
threshold 0.5 / neg 0.35, end of speech after MIN_SILENCE of silence, PAD on both sides, and at MAX_SPEECH the
segment is cut at the last pause longer than 98 ms (hard cut only if there was none).
Feed 512-sample (32 ms) windows; get back events: ("start", t) / ("end", t_speech_end).
"""
import os
import numpy as np
import onnxruntime

SR = 16000
WIN = 512
CTX = 64
MODEL = os.path.join(os.path.dirname(__file__), "..", "models", "silero_vad_v6.onnx")


class StreamVad:
    def __init__(self, threshold=0.5, min_silence=0.5, pad=0.2, max_speech=25.0):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.sess = onnxruntime.InferenceSession(MODEL, providers=["CPUExecutionProvider"], sess_options=opts)
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros((1, CTX), np.float32)
        self.thr, self.neg = threshold, max(threshold - 0.15, 0.01)
        self.min_sil, self.pad = min_silence, pad
        self.max_speech = max_speech - WIN / SR - 2 * pad     # same effective cap as the offline function
        self.n = 0                 # samples consumed
        self.speaking = False
        self.start = 0.0
        self.temp_end = None       # first silent window after speech (candidate end)
        self.prev_end = None       # last pause > 98 ms inside the current segment (cut point at the cap)
        self.next_start = None     # where speech resumed after prev_end

    def prob(self, win):
        x = np.concatenate([self.ctx, win[None, :]], 1)
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = win[None, -CTX:]
        return float(np.asarray(out).reshape(-1)[0])

    def feed(self, win):
        """win: 512 float32 samples. Returns list of events (kind, t_sec)."""
        assert len(win) == WIN
        p = self.prob(win)
        t = self.n / SR
        self.n += WIN
        ev = []
        if p >= self.thr and self.temp_end is not None:
            self.temp_end = None
            if self.prev_end is not None and (self.next_start is None or self.next_start < self.prev_end):
                self.next_start = t
        if p >= self.thr and not self.speaking:
            self.speaking, self.start = True, t
            self.prev_end = self.next_start = self.temp_end = None
            ev.append(("start", max(0.0, t - self.pad)))
            return ev
        if self.speaking and t - self.start > self.max_speech:
            if self.prev_end is not None:
                ev.append(("end", self.prev_end))
                if self.next_start is not None and self.next_start > self.prev_end:
                    self.start = self.next_start
                    ev.append(("start", max(0.0, self.next_start - self.pad)))
                else:
                    self.speaking = False
            else:
                ev.append(("end", t))
                self.speaking = False
            self.prev_end = self.next_start = self.temp_end = None
            return ev
        if p < self.neg and self.speaking:
            if self.temp_end is None:
                self.temp_end = t
            if t - self.temp_end > 0.098:
                self.prev_end = self.temp_end
            if t - self.temp_end >= self.min_sil:
                ev.append(("end", self.temp_end))
                self.speaking = False
                self.prev_end = self.next_start = self.temp_end = None
        return ev

    def flush(self):
        if self.speaking:
            self.speaking = False
            return [("end", self.temp_end if self.temp_end is not None else self.n / SR)]
        return []


if __name__ == "__main__":  # self-check against the offline reference on the public file
    import sys
    import soundfile as sf
    sys.path.insert(0, os.path.dirname(__file__))
    from faster_whisper.vad import get_speech_timestamps, VadOptions  # only for this check (needs faster-whisper)
    wav = sys.argv[1] if len(sys.argv) > 1 else "run/b1_16k.wav"
    a, sr = sf.read(wav, dtype="float32")
    v = StreamVad(); ev = []
    for i in range(0, len(a) // WIN * WIN, WIN):
        ev += v.feed(a[i:i + WIN])
    ev += v.flush()
    starts = [t for k, t in ev if k == "start"]
    off = get_speech_timestamps(a, VadOptions(min_silence_duration_ms=500, speech_pad_ms=200, max_speech_duration_s=25))
    print("stream segments", len(starts), "offline", len(off))
    print("start diff > 40 ms:", sum(abs(s - o["start"] / sr) > 0.04 for s, o in zip(starts, off)))
