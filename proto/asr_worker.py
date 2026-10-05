"""ASR worker process. stdin: PCM frames (common.py). stdout: JSON events, one per line.

One Qwen3-ASR model object serves both passes (design 1.3):
  pass 1  streaming, 2 s chunks, partial text while speech is going on
  pass 2  VAD segments merged to >= GROUP s (or a >= LONG_GAP s pause), re-run offline -> final text
Priority: incoming audio (pass 1) first; queued pass-2 groups run only when no frame is waiting (design 1.8, ASR queue).
Also in this process: streaming Silero VAD (vad.py), language filter + glossary (glossary.py),
online speaker labelling on the far-end stream with sherpa-onnx (design 1.5a step 1).
"""
import argparse
import collections
import json
import os
import queue
import resource
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from common import SR, STREAMS, read_frame, emit  # noqa: E402
from vad import StreamVad, WIN  # noqa: E402
from glossary import Glossary, language_drop, cjk_ratio, build_context, context_leak  # noqa: E402
from speakers import CentroidSpeakers  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run-dir")
ap.add_argument("--warm", default=None, help="Lỗi 15: load + warm up now, touch this file, then read the meeting settings as ONE JSON line on stdin")
ap.add_argument("--language", default="English")
ap.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
ap.add_argument("--glossary", default=os.path.join(os.path.dirname(__file__), "glossary.json"))
ap.add_argument("--chunk", type=float, default=2.0)
ap.add_argument("--group", type=float, default=15.0)
ap.add_argument("--long-gap", type=float, default=2.0)
ap.add_argument("--min-silence", type=float, default=0.5)
ap.add_argument("--pad", type=float, default=0.2)
ap.add_argument("--max-speech", type=float, default=25.0)
ap.add_argument("--speaker-threshold", type=float, default=0.45)
ap.add_argument("--min-embed-s", type=float, default=2.0)
ap.add_argument("--t0", type=float, default=0.0, help="absolute audio time of the first sample (window start)")
A = ap.parse_args()
MODELS = os.path.join(os.path.dirname(__file__), "..", "models")
RING_S = 120

import mlx.core as mx  # noqa: E402
from mlx_qwen3_asr import load_model, transcribe  # noqa: E402
from mlx_qwen3_asr.streaming import init_streaming, feed_audio, finish_streaming  # noqa: E402
import sherpa_onnx  # noqa: E402

t0 = time.time()
model, _cfg = load_model(A.model)
transcribe(np.zeros(SR, np.float32), model=model, language=A.language)
st = init_streaming(model=A.model, chunk_size_sec=A.chunk, language=A.language)
feed_audio(np.zeros(SR * 2, np.float32), st, model=model); mx.eval()
ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
    model=os.path.join(MODELS, "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"), num_threads=2))
if A.warm:                                       # Lỗi 15: kept warm by the daemon until a meeting takes this worker
    open(A.warm, "w").close()
    line = sys.stdin.buffer.readline()
    if not line:                                 # the daemon went away before any meeting
        os.remove(A.warm); sys.exit(0)
    for k, v in json.loads(line).items():       # run_dir, language, t0, glossary, max_speech
        setattr(A, k, v)
    t0 = time.time()                             # load_s of the "ready" event = time from the hand-over, not the idle wait
spk_mgr = CentroidSpeakers(threshold=A.speaker_threshold)   # running centroids, matches the calibration in report 1.4
glossary = Glossary(A.glossary)
def net_selftest():
    """Positive control for the network guard: an outbound TCP attempt must FAIL (EPERM under sandbox-exec)."""
    import socket
    out = []
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1.0).close(); out.append("tcp:OPEN")
    except OSError as e:
        out.append(f"tcp:blocked:{e.errno}")
    try:
        socket.getaddrinfo("apple.com", 443); out.append("dns:OPEN")          # resolver socket = outbound channel (rnd M1 item 2)
    except OSError as e:
        out.append(f"dns:blocked:{e.errno}")
    return " ".join(out)


emit({"type": "ready", "load_s": round(time.time() - t0, 2), "gpu_active_gb": round(mx.get_active_memory() / 1e9, 2), "net_selftest": net_selftest()})


class Ring:
    """Fixed ring of the last RING_S seconds; sample k of the stream lives at k % size."""
    def __init__(self):
        self.size = RING_S * SR
        self.buf = np.zeros(self.size, np.float32)
        self.n = 0

    def append(self, x):
        idx = (np.arange(len(x)) + self.n) % self.size
        self.buf[idx] = x
        self.n += len(x)

    def slice(self, t0, t1):
        i0, i1 = max(0, int(t0 * SR)), min(self.n, int(t1 * SR))
        assert i0 >= self.n - self.size, "ring too small for this group"
        return self.buf[np.arange(i0, i1) % self.size]


class Stream:
    def __init__(self, sid):
        self.sid, self.name = sid, STREAMS[sid]
        self.vad = StreamVad(min_silence=A.min_silence, pad=A.pad, max_speech=A.max_speech)
        self.ring = Ring()
        self.vad.n = self.ring.n = int(A.t0 * SR)   # all times are absolute (same clock as the slide reader)
        self.vad_buf = np.zeros(0, np.float32)
        self.speaking = False
        self.seg_start = 0.0
        self.p1 = None            # streaming state of the current utterance
        self.p1_fed_until = 0.0   # audio time fed to pass 1
        self.group = None         # {"start", "end", "first_end", "segs": [(s, e, label)]}
        self.n_seg = 0
        self.lang = A.language    # language of the current utterance (Lỗi 10: may change mid-meeting)


streams = {0: Stream(0), 1: Stream(1)}
pending = collections.deque()      # closed groups waiting for pass 2
slides = []                        # [{"t", "words"}] tailed from <run-dir>/slides.jsonl
slides_fd = None
group_id = 0
n_speakers = 0
frames = queue.Queue()


def reader():
    fd = sys.stdin.buffer
    while True:
        fr = read_frame(fd)
        frames.put(fr)
        if fr is None:
            return


threading.Thread(target=reader, daemon=True).start()


def tail_slides():
    global slides_fd
    p = os.path.join(A.run_dir, "slides.jsonl")
    if slides_fd is None and os.path.exists(p):
        slides_fd = open(p)
    if slides_fd:
        for line in slides_fd:
            if line.endswith("\n"):
                slides.append(json.loads(line))


def slide_words_at(t):
    tail_slides()
    ks = [s for s in slides if s["t"] <= t + 2]
    return ks[-1]["words"] if ks else []


def label_speaker(s, audio, t_end):
    """Online labelling (design 1.5a step 1): embedding -> nearest known speaker or a new 'Speaker N'. The embedding goes out with
    seg_end: the daemon's rolling diarization reuses it instead of loading CAM++ a second time (05/10)."""
    global n_speakers
    if s.sid != 0 or len(audio) < A.min_embed_s * SR:
        return "", 0.0, None
    t = time.time()
    stm = ext.create_stream(); stm.accept_waveform(SR, audio); stm.input_finished()
    emb = np.array(ext.compute(stm))
    name = spk_mgr.label(emb)
    n_speakers = len(spk_mgr)
    return name, round(time.time() - t, 3), [round(float(v), 5) for v in emb]


# Lỗi 10: the daemon writes the newly chosen language to run_dir/asr_language (set_language). It is read at each speech START,
# so an utterance keeps the language it started with and finished text is never re-recognised.
LANG_PATH = os.path.join(A.run_dir, "asr_language")
lang_state = {"mtime": None, "lang": A.language}


def current_language():
    try:
        m = os.path.getmtime(LANG_PATH)
    except OSError:
        return lang_state["lang"]
    if m != lang_state["mtime"]:
        lang_state["mtime"] = m
        new = open(LANG_PATH).read().strip()
        if new and new != lang_state["lang"]:
            emit({"type": "language_applied", "language": new, "previous": lang_state["lang"], "t_emit": time.time()})
            lang_state["lang"] = new
    return lang_state["lang"]


def emit_partial(s, final, t_audio, speech_end=None):
    dropped = language_drop(s.p1.text, s.lang)     # design 1.4 applies to the displayed partial text too
    emit({"type": "partial", "stream": s.name, "t_audio": round(t_audio, 3), "t_emit": time.time(), "speech_end": speech_end,
          "text": "" if dropped else s.p1.text, "stable": "" if dropped else s.p1.stable_text, "final": final,
          "dropped_lang": dropped, "lag_s": round(cur_lag, 3), "gpu_active_gb": round(mx.get_active_memory() / 1e9, 2)})


def pass1_feed(s, t_now):
    """Feed whole chunks of the current utterance to the streaming state."""
    while s.speaking and t_now - s.p1_fed_until >= A.chunk:
        t1 = s.p1_fed_until + A.chunk
        x = s.ring.slice(s.p1_fed_until, t1)
        tc = time.time()
        feed_audio(x, s.p1, model=model); mx.eval()
        s.p1_fed_until = t1
        emit_partial(s, False, t1)
        s.p1_compute = getattr(s, "p1_compute", 0.0) + time.time() - tc


def close_group(s, wait):
    global group_id
    g = s.group; s.group = None
    group_id += 1
    g.update(id=group_id, stream=s.name, wait=wait, t_closed=time.time())
    pending.append(g)


def on_vad(s, kind, t, t_now):
    if kind == "start":
        lang = current_language()
        if s.group is not None and s.group["language"] != lang:   # language switched: finish the open group in its own language
            close_group(s, 0.0)
        s.speaking, s.seg_start, s.lang = True, t, lang
        s.p1 = init_streaming(model=A.model, chunk_size_sec=A.chunk, language=lang,
                              context=build_context(glossary.context_terms(), slide_words_at(t)))
        s.p1_fed_until = t
        emit({"type": "seg_start", "stream": s.name, "t": round(t, 3), "t_emit": time.time()})
        return
    # end of speech: t = last speech sample; segment keeps PAD after it (same as offline VAD)
    e = min(t + A.pad, t_now)
    s.speaking = False
    if e > s.p1_fed_until:
        feed_audio(s.ring.slice(s.p1_fed_until, e), s.p1, model=model)
    finish_streaming(s.p1, model=model); mx.eval()
    emit_partial(s, True, e, speech_end=t)
    seg_audio = s.ring.slice(s.seg_start, e)
    label, emb_s, emb = label_speaker(s, seg_audio, e)
    s.n_seg += 1
    emit({"type": "seg_end", "stream": s.name, "s": round(s.seg_start, 3), "e": round(e, 3), "speech_end": round(t, 3),
          "speaker": label, "embed_s": emb_s, "emb": emb, "p1_text": s.p1.text, "p1_leak": context_leak(s.p1.text, s.p1.context),
          "p1_dropped": language_drop(s.p1.text, s.lang), "t_emit": time.time()})
    if s.group is None:
        s.group = {"start": s.seg_start, "end": e, "first_end": t, "segs": [], "p1_texts": [], "language": s.lang}
    s.group["end"] = e
    s.group["speech_end"] = t
    s.group["segs"].append((s.seg_start, e, label))
    s.group["p1_texts"].append(s.p1.text)
    if s.group["end"] - s.group["start"] >= A.group:
        close_group(s, A.min_silence)


cur_lag = 0.0   # queue delay of the frame being processed = now - wall clock when the player sent it


LEVEL_S = 5.0


def process(sid, t_end, t_sent, x):
    global cur_lag
    cur_lag = time.time() - t_sent
    s = streams[sid]
    s.ring.append(x)
    s.lvl_e = getattr(s, "lvl_e", 0.0) + float(np.sum(x.astype(np.float64) ** 2)); s.lvl_n = getattr(s, "lvl_n", 0) + len(x)
    s.lvl_peak = max(getattr(s, "lvl_peak", 0.0), float(np.abs(x).max()) if len(x) else 0.0)
    if not hasattr(s, "lvl_t0"):
        s.lvl_t0, s.lvl_w0 = t_end - len(x) / SR, t_sent
    if s.lvl_n >= LEVEL_S * SR:      # signal level + capture-rate diagnostics, numbers only
        rms = (s.lvl_e / s.lvl_n) ** 0.5
        rate = (t_end - s.lvl_t0) / max(1e-6, t_sent - s.lvl_w0)     # audio seconds delivered per wall second (must be ~1.0)
        emit({"type": "level", "stream": s.name, "t_audio": round(t_end, 2), "rms_dbfs": round(20 * np.log10(rms + 1e-9), 1),
              "peak": round(s.lvl_peak, 4), "capture_rate": round(rate, 3), "t_emit": time.time()})
        s.lvl_e, s.lvl_n, s.lvl_peak = 0.0, 0, 0.0
        s.lvl_t0, s.lvl_w0 = t_end, t_sent
    s.vad_buf = np.concatenate([s.vad_buf, x])
    n = len(s.vad_buf) // WIN * WIN
    for i in range(0, n, WIN):
        t_now = s.vad.n / SR + WIN / SR
        for kind, t in s.vad.feed(s.vad_buf[i:i + WIN]):
            on_vad(s, kind, t, t_now)
    s.vad_buf = s.vad_buf[n:]
    t_now = s.vad.n / SR
    pass1_feed(s, t_now)
    if s.group is not None and not s.speaking and t_now - s.group["speech_end"] >= A.long_gap:
        close_group(s, A.long_gap)


glossary_mtime = os.path.getmtime(A.glossary)


def pass2(g):
    global glossary, glossary_mtime
    try:                                             # hot reload: the daemon rewrites the file on glossary_add
        m = os.path.getmtime(A.glossary)
        if m != glossary_mtime:
            glossary, glossary_mtime = Glossary(A.glossary), m
    except OSError:
        pass
    audio = streams[0 if g["stream"] != "mic" else 1].ring.slice(g["start"], g["end"])
    words = slide_words_at(g["start"])
    ctx = build_context(glossary.context_terms(), words)
    tc = time.time()
    r = transcribe(audio, model=model, language=g["language"], context=ctx)
    compute = time.time() - tc
    raw = r.text.strip()
    dropped = language_drop(raw, g["language"])
    leak = context_leak(raw, ctx)
    text = raw
    leak_both = False
    if leak:   # leak -> fall back to pass-1 text; pass 1 had the same context, so check it again (design 1.7: drop + flag)
        text = " ".join(t for t in g["p1_texts"] if t).strip()
        if context_leak(text, ctx):
            text, leak_both = "", True
    text, n_corr = glossary.correct(text)
    secs = collections.Counter()
    for s0, e0, lab in g["segs"]:
        if lab:
            secs[lab] += e0 - s0
    speaker = secs.most_common(1)[0][0] if secs else ""
    emit({"type": "final", "id": g["id"], "stream": g["stream"], "s": round(g["start"], 3), "e": round(g["end"], 3),
          "speech_end": round(g["speech_end"], 3), "first_end": round(g["first_end"], 3), "wait": g["wait"],
          "t_closed": g["t_closed"], "t_emit": time.time(), "compute_s": round(compute, 3),
          "text": text, "text_raw": raw, "n_words": len(text.split()), "cjk_ratio": round(cjk_ratio(raw), 3),
          "dropped_lang": dropped, "ctx": ctx, "ctx_leak": leak, "ctx_leak_both": leak_both, "n_corrections": n_corr,
          "speaker": speaker, "n_segs": len(g["segs"]), "lang": r.language, "asr_language": g["language"],
          "queue_after": len(pending), "gpu_active_gb": round(mx.get_active_memory() / 1e9, 2)})


p2_compute = 0.0
while True:
    try:
        fr = frames.get(timeout=0.002 if pending else None)
    except queue.Empty:
        g = pending.popleft(); t = time.time(); pass2(g); p2_compute += time.time() - t
        continue
    if fr is None:
        break
    process(*fr)

for s in streams.values():
    t_now = s.vad.n / SR
    for kind, t in s.vad.flush():
        on_vad(s, kind, t, t_now)
    if s.group is not None:
        close_group(s, 0.0)
while pending:
    g = pending.popleft(); t = time.time(); pass2(g); p2_compute += time.time() - t
emit({"type": "eof", "t_emit": time.time(), "n_speakers_online": n_speakers, "n_groups": group_id,
      "n_segs": {s.name: s.n_seg for s in streams.values()},
      "p1_compute_s": round(sum(getattr(s, "p1_compute", 0.0) for s in streams.values()), 1),
      "p2_compute_s": round(p2_compute, 1),
      "peak_gpu_gb": round(mx.get_peak_memory() / 1e9, 2),
      "max_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2)})
