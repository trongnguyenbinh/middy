"""Midy core: one meeting = one Session. Owns the audio source, the ASR worker, the LLM worker, the slide reader,
the SQLite store, the rolling notes and the measurements. Used by run_m0.py (measurement CLI) and midyd.py (daemon).

Event vocabulary follows the reference app's session messages where known (design decision 25/09): every transcript event carries
`source` = "mic" | "system" (the reference app: source: mic/system). Persisted rows are written the moment a final arrives.
"""
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(__file__))
from common import SR  # noqa: E402
from glossary import CJK, output_language  # noqa: E402
import ask as ask_ctx  # noqa: E402
import stamps  # noqa: E402
import mom_c  # noqa: E402
from sources import FilePlayer, LiveCapture, UiSource  # noqa: E402
from slides import SlideWatcher  # noqa: E402
from store import Store  # noqa: E402
from blocks import Transcript, words  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "..", ".venv", "bin", "python")
ENV = dict(os.environ, HF_HOME=os.environ.get("MIDY_HF_HOME", os.path.join(HERE, "..", "models", "hf")), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
PROMPTS = {k: open(os.path.join(HERE, "prompts", f"{k}.md")).read() for k in ("live_summary", "ask")}   # MoM: mom_c.py (Lỗi 21c)
LIVE_MAX_TOKENS = int(os.environ.get("MIDY_LIVE_MAX_TOKENS", 400))   # output cap for the live note (local-LLM consequence; review M1 rnd-8 / CEO C6; 300 -> 400 rnd v2 item 5)
# the reference app auto summary: AUTO_SUMMARY_DEBOUNCE_MS = 5e3, AUTO_SUMMARY_WORD_THRESHOLD = 30, AUTO_SUMMARY_MIN_READ_MS = 1e4
SUM_DEBOUNCE_S, SUM_WORDS, SUM_MIN_GAP_S = 5.0, 30, 10.0      # SUM_DEBOUNCE_S / SUM_WORDS: the reference app rule used until Việc 20
# Việc 20 (anh 30/09, "máy nóng khi ghi"): Gemma writing the live notes was most of the GPU during a meeting (Việc 19: ~11 GPU-s/min).
# The live notes are now rewritten on a clock: at most once every LIVE_EVERY_S seconds, with every sentence refined since the last
# round (none is dropped: take_unsummarised + the end-of-meeting flush). The MoM, Ask and the Word export read the same notes/text.
LIVE_EVERY_S = float(os.environ.get("MIDY_LIVE_EVERY_S", 120))
LAG_NOTE_S = 1.0           # partial lag above this is logged with the LLM state (rnd v2 item 6)
IDLE_S = 20.0            # the reference app: app releases mic and audio silent >= 20 s -> 20 s countdown -> auto end (spec A5)
mmss = lambda x: f"{int(x // 60):02d}:{int(x % 60):02d}"
pct = lambda xs, q: round(float(np.percentile(xs, q)), 2) if len(xs) else None


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def swap_used_gb():
    return float(sh(["sysctl", "-n", "vm.swapusage"]).split("used = ")[1].split("M")[0]) / 1024


def pageouts():
    return int([l for l in sh(["vm_stat"]).splitlines() if l.startswith("Pageouts")][0].split()[-1].rstrip("."))


def memlevel():
    return int(sh(["sysctl", "-n", "kern.memorystatus_level"]) or 0)


def input_volume():
    """macOS input volume 0-100 (read-only; rnd 5.1.3). None if osascript is unavailable (e.g. under the sandbox)."""
    try:
        return int(sh(["osascript", "-e", "input volume of (get volume settings)"]).strip() or -1)
    except (ValueError, OSError):
        return None


import ctypes  # noqa: E402
_libproc = None                  # loaded on first use: importing core (tests, Linux CI) must not need macOS libproc


def proc_rss_gb(pid):
    """Resident size via proc_pidinfo(PROC_PIDTASKINFO); `ps` cannot be executed under sandbox-exec."""
    global _libproc
    if _libproc is None:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    buf = ctypes.create_string_buffer(256)
    if _libproc.proc_pidinfo(pid, 4, 0, buf, 256) < 16:
        return None
    import struct
    return round(struct.unpack_from("<Q", buf, 8)[0] / 1e9, 2)


DEFAULTS = dict(name="meeting", input=None, live=False, start=0.0, end=None, language="English", speed=1.0, llm_policy="guard",
                no_llm=False, no_slides=False, chunk_min=10.0, diar_threshold=0.8, num_speakers=-1, space="default",
                run_dir=None, db=None, resume=None, auto_end=False, vad_max_speech=25.0, duration=None, capture="audiotee", mic_offset=0.0)


LANGUAGES = ("English", "Vietnamese", "Chinese", "Japanese", "Korean", "German", "French", "Spanish")   # the UI's list


class Session:
    def __init__(self, **cfg):
        self.cfg = c = {**DEFAULTS, **cfg}
        self.run_dir = c["run_dir"] or os.path.join(HERE, "..", "run", c["name"])
        os.umask(0o077)                                            # meeting data: files 600, dirs 700 (CEO C2)
        os.makedirs(self.run_dir, exist_ok=True); os.chmod(self.run_dir, 0o700)
        if not c.get("resume"):                                   # Lỗi 10: a language switch of an older meeting in the same dir must not leak in
            try:
                os.remove(os.path.join(self.run_dir, "asr_language"))
            except OSError:
                pass
        self.store = Store(c["db"] or os.path.join(HERE, "..", "run", "midy.db"))
        for f in (c["db"] or os.path.join(HERE, "..", "run", "midy.db"),):
            if os.path.exists(f):
                os.chmod(f, 0o600)
        self.subscribers = []
        self.lock = threading.Lock()
        self.stop_source = threading.Event()
        self.state = "init"
        self.finals, self.partials, self.segs = [], [], []
        self.part_ids, self.next_chunk = [], 0
        self.llm_done, self.llm_paused, self.llm = {}, False, None
        # Lỗi 14: a stopped meeting finishes in the background while the next one records. Its Gemma is paused whenever the NEW
        # meeting's ASR lags (ext_pause, set through the daemon's on_pause hook). Lỗi 14b (anh: "máy đủ ram, đừng chờ mom viết xong"):
        # the new meeting loads its own Gemma at once, next to the old one's.
        self.own_pause, self.ext_pause, self.on_pause, self.llm_quit = False, False, None, False
        self.llm_thread, self._llm_proc, self.ready = None, None, {}
        self.pids, self.extra_pids, self.net_lines = {"orchestrator": os.getpid()}, set(), []
        self.sys_audio = []          # captured system-audio frames (live) for diarization
        self.last_speech_wall = time.time()
        self.idle_sent = False
        self.diar = None
        self.meeting_id = None
        self.blocks_fd = open(os.path.join(self.run_dir, "blocks.jsonl"), "a")
        self.window_fd = open(os.path.join(self.run_dir, "window_events.jsonl"), "a")   # replayed by client_check.py
        self.tx = Transcript(on_window=self._on_window)
        self.live_note, self.new_final_words, self.last_summary_end, self.sum_timer, self.sum_inflight, self.n_live = "", 0, 0.0, None, False, 0
        self.sum_rows, self.live_truncated, self.live_truncated_kept = [], 0, 0   # in-flight blocks; truncated notes discarded / kept
        self.sum_streak = 0                            # consecutive truncated summaries (the second one is kept, blocks never wait twice)
        self.asks, self.n_ask = {}, 0                   # Việc 17: questions asked during the meeting (id -> question + timings)
        self.stamped = {}                               # Lỗi 21: lines stamped from the transcript / lines considered, per note kind
        self.lag_peaks = []                            # partial lag > LAG_NOTE_S with the LLM state at that moment (rnd v2 item 6)
        self.llm_sent = set()
        self.levels = {}
        self.rates = {}
        self.seq_base = 0

    # ---- pub/sub ---------------------------------------------------------------------------------------------
    def subscribe(self):
        """New subscriber first gets a snapshot of the whole transcript (the reference app: formatted_transcript / hydrate)."""
        q = queue.Queue(maxsize=10000)
        q.put_nowait(self.tx.snapshot())
        self.subscribers.append(q); return q

    def publish(self, ev):
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except queue.Full:                                 # slow client: drop its backlog, tell it to resync (rnd M1 item 5)
                with q.mutex:
                    q.queue.clear()
                q.put_nowait({"type": "resync"})

    def _on_window(self, ev):
        self.window_fd.write(json.dumps(ev, ensure_ascii=False) + "\n"); self.window_fd.flush()
        self.publish(ev)

    def _out_language(self):
        """Lỗi 11: language of the live notes and the MoM (the chosen one, or Vietnamese when the recognised text is)."""
        return output_language([f["text"] for f in self.finals], self.cfg["language"])

    def set_language(self, lang):
        """Lỗi 10: switch the recognition language during a meeting. The ASR worker applies it to utterances that START after
        this (it reads run_dir/asr_language at each speech start); finished text is never re-recognised. Live notes and the MoM
        are written in the new language from now on (cfg["language"] feeds their prompts)."""
        if lang not in LANGUAGES:
            return {"ok": False, "error": "unknown language"}
        old = self.cfg["language"]
        self.cfg["language"] = lang
        tmp = os.path.join(self.run_dir, "asr_language.tmp")
        with open(tmp, "w") as f:
            f.write(lang)
        os.replace(tmp, os.path.join(self.run_dir, "asr_language"))
        if getattr(self, "meeting_id", None):
            self.store.set_language(self.meeting_id, lang)
        self.publish({"type": "language", "language": lang, "previous": old})
        return {"ok": True, "language": lang, "previous": old}

    def status(self):
        return {"state": self.state, "meeting_id": self.meeting_id, "audio_s": round(getattr(self.source, "t_last", 0.0) - self.cfg["start"], 1) if hasattr(self, "source") else 0,
                "finals": len(self.finals), "partials": len(self.partials), "parts": len(self.part_ids), "llm_paused": self.llm_paused,
                "speakers_online": max([len({f["speaker"] for f in self.finals if f["speaker"]})] or [0]), "levels": self.levels}

    # ---- 1. prepare ------------------------------------------------------------------------------------------
    def _prepare(self):
        c = self.cfg
        for f in ("slides.jsonl", "ocr.tsv", "events.jsonl", "llm_events.jsonl", "samples.jsonl"):
            open(os.path.join(self.run_dir, f), "a").close()
        self.audio, self.has_video, self.mic_audio = None, False, None
        if c["resume"]:
            m = self.store.meeting(c["resume"])
            if not m:                                     # not an assert: `python -O` would skip it
                raise ValueError("unknown meeting id")
            self.meeting_id = m["id"]
            src = json.loads(m["source"]); c["input"], c["end"] = src.get("file"), src.get("end")
            c["language"], c["space"], c["num_speakers"] = m["language"], m["space"], m["num_speakers"]
            self.finals = [dict(x, id=x["seq"], dropped_lang=bool(x["dropped_lang"]), ctx_leak=bool(x["ctx_leak"])) for x in self.store.segments(m["id"])]
            resume_from = max([f["e"] for f in self.finals] or [src.get("start", 0.0)])
            self.seq_base = max([f["id"] for f in self.finals] or [0])     # new worker counts groups from 1 again
            c["start"] = resume_from                      # continue right after the last committed group
            self.part_ids = [f"part{n['idx']}" for n in self.store.notes(m["id"], "part")]
            for n in self.store.notes(m["id"], "part"):
                self.llm_done[f"part{n['idx']}"] = {"text": n["text"], "stats": n["stats"]}
            self.next_chunk = len(self.part_ids)
            live = self.store.notes(m["id"], "live")
            if live:
                self.live_note, self.n_live = stamps.clean(live[-1]["text"]), live[-1]["idx"] + 1   # Gemma sees notes without times
            self.store.set_status(m["id"], "recording")
        else:
            self.meeting_id = self.store.new_meeting(c["name"], c["space"], c["language"], {"file": c["input"], "start": c["start"], "end": c["end"], "live": c["live"]}, c["num_speakers"])
        self.store.set_run_dir(self.meeting_id, os.path.realpath(self.run_dir))            # so a later delete can remove the files too
        self.origin = json.loads(self.store.meeting(self.meeting_id)["source"]).get("start", 0.0)   # chunk grid anchor
        if not c["live"]:
            wav = os.path.join(self.run_dir, "audio.wav")
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(c["start"])]
            if c["end"]:
                cmd += ["-to", str(c["end"])]
            subprocess.run(cmd + ["-i", c["input"], "-vn", "-ac", "1", "-ar", str(SR), wav], check=True)
            self.audio, sr = sf.read(wav, dtype="float32"); assert sr == SR
            if c["mic_offset"]:                    # two-stream regression: the mic hears the same recording shifted by mic_offset s
                mwav = os.path.join(self.run_dir, "mic.wav")
                cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(c["start"] + c["mic_offset"]), "-t", str(len(self.audio) / SR)]
                subprocess.run(cmd + ["-i", c["input"], "-vn", "-ac", "1", "-ar", str(SR), mwav], check=True)
                self.mic_audio, _ = sf.read(mwav, dtype="float32")
            self.has_video = "video" in sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", c["input"]])
        gpath = os.path.join(self.run_dir, "glossary.json")     # worker reads the merged glossary of this space
        json.dump(self.store.glossary(c["space"]), open(gpath, "w"), ensure_ascii=False)
        self.glossary_path = gpath
        self.baseline = {"swap_used_gb": round(swap_used_gb(), 2), "pageouts": pageouts(), "memorystatus_level": memlevel(), "t": time.time(),
                         "input_volume": input_volume()}

    # ---- 2. workers ------------------------------------------------------------------------------------------
    # Lỗi 15: the daemon keeps one ASR and one Gemma worker loaded and warmed up (cfg "pool", see midyd.Pool). A meeting takes them
    # and only hands over its settings; a worker that died or does not answer (e.g. after sleep) is replaced by a cold one, never waited on.
    LOAD_TIMEOUT_S = 90                           # a worker still loading: a cold load measured 6-40 s
    ANSWER_TIMEOUT_S = 10                         # a worker that says it is loaded answers in < 1 s; silence = hung (sleep?) -> replace

    def _spawn(self):
        c = self.cfg
        self.warm = {"asr": False, "llm": False}
        self.llm_sock = None
        if not c["no_llm"]:                       # loads next to the ASR model
            self.llm_thread = threading.Thread(target=self._spawn_llm, daemon=True); self.llm_thread.start()
        meeting = {"run_dir": self.run_dir, "language": c["language"], "t0": c["start"], "glossary": self.glossary_path, "max_speech": c["vad_max_speech"]}
        w = c["pool"].take("asr") if c.get("pool") else None
        if w:
            p, flag = w
            loaded = os.path.exists(flag)             # the worker touches it once loaded + warmed up
            try:
                os.remove(flag)
            except OSError:
                pass
            try:
                p.stdin.write((json.dumps(meeting) + "\n").encode()); p.stdin.flush()
                self.ready["asr"] = self._read_ready(p, self.ANSWER_TIMEOUT_S if loaded else self.LOAD_TIMEOUT_S)
                self.asr, self.warm["asr"] = p, True
            except (OSError, TimeoutError, ValueError):
                p.kill()
        if not self.warm["asr"]:
            self.asr = subprocess.Popen([PY, os.path.join(HERE, "asr_worker.py"), "--run-dir", self.run_dir, "--language", c["language"],
                                         "--t0", str(c["start"]), "--glossary", self.glossary_path, "--max-speech", str(c["vad_max_speech"])],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.path.join(self.run_dir, "asr.err"), "w"), env=ENV, bufsize=0)
            self.ready["asr"] = json.loads(self.asr.stdout.readline().decode())
        self.pids["asr"] = self.asr.pid
        if self.ready["asr"].get("type") != "ready":
            raise RuntimeError(f"asr worker did not start: {self.ready['asr']}")
        if self.llm_thread:                       # "ready" waits for Gemma too
            self.llm_thread.join()
            if not self.llm:
                raise RuntimeError("llm worker died, see llm.err")
        self.publish({"type": "ready", **self.ready})

    def _read_ready(self, w, timeout):
        import select
        if not select.select([w.stdout], [], [], timeout)[0]:
            raise TimeoutError("warm ASR worker did not answer")
        return json.loads(w.stdout.readline().decode())

    def _spawn_llm(self):
        w = self.cfg["pool"].take("llm") if self.cfg.get("pool") else None
        if w and self._connect_llm(*w):
            self.warm["llm"] = True
        else:
            if w:                                 # warm worker hung (e.g. after sleep): replace it
                w[0].kill()
                try:
                    os.remove(w[1])
                except OSError:
                    pass
            sp = os.path.join(self.run_dir, "llm.sock")
            if os.path.exists(sp):
                os.unlink(sp)
            p = subprocess.Popen([PY, os.path.join(HERE, "llm_worker.py"), "--socket", sp], stderr=open(os.path.join(self.run_dir, "llm.err"), "w"), env=ENV)
            if not self._connect_llm(p, sp, warm=False):
                return                            # died: no notes / MoM; _finish marks the meeting as an error
        self.llm_events = open(os.path.join(self.run_dir, "llm_events.jsonl"), "a")
        self.llm_reader_t = threading.Thread(target=self._llm_reader, daemon=True); self.llm_reader_t.start()
        self.llm = self._llm_proc                 # usable from here on
        self._set_pause()                         # apply a pause requested while it was loading

    def _connect_llm(self, p, sp, warm=True):
        """Wait for the worker's socket (it binds once the model is loaded), connect, read its "ready" line. False = unusable."""
        self._llm_proc = p; self.pids["llm"] = p.pid
        t = time.time()
        while not os.path.exists(sp):
            if p.poll() is not None or (warm and time.time() - t > self.LOAD_TIMEOUT_S):
                return False
            time.sleep(0.05)
        try:
            self.llm_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.llm_sock.settimeout(self.ANSWER_TIMEOUT_S if warm else None); self.llm_sock.connect(sp)
            self.llm_r, self.llm_w = self.llm_sock.makefile("r"), self.llm_sock.makefile("w")
            self.ready["llm"] = json.loads(self.llm_r.readline())
        except (OSError, ValueError):
            return False
        self.llm_sock.settimeout(None)
        if warm:
            os.remove(sp)                         # connected; the warm socket name is not needed any more
        return True

    def cleanup(self):
        for p in (getattr(self, "asr", None), self.llm, self._llm_proc):
            if p is not None and p.poll() is None:
                p.kill()

    # ---- 3. sampler + network check ------------------------------------------------------------------------------
    def _sampler(self):
        k = 0
        f = open(os.path.join(self.run_dir, "samples.jsonl"), "a")
        while not self.stop_sampler.is_set():
            allp = list(self.pids.values()) + list(self.extra_pids)
            rss = {}
            for pid in allp:
                r = proc_rss_gb(pid)
                if r is not None:
                    rss[next((n for n, p in self.pids.items() if p == pid), f"pid{pid}")] = r
            rec = {"t": time.time(), "rss_gb": rss, "loadavg_1m": round(os.getloadavg()[0], 1)}
            if k % 5 == 0:
                rec.update(swap_used_gb=round(swap_used_gb(), 2), pageouts=pageouts(), memorystatus_level=memlevel())
            if k % 15 == 0:
                out = sh(["lsof", "-nP", "-i", "-a", "-p", ",".join(map(str, allp))])
                lines = [l for l in out.splitlines() if l and not l.startswith("COMMAND")]
                rec["lsof_i_lines"] = len(lines); self.net_lines.extend(lines)
            f.write(json.dumps(rec) + "\n"); f.flush(); k += 1
            self.stop_sampler.wait(1.0)

    # ---- 4. source -------------------------------------------------------------------------------------------
    def _start_source(self):
        c = self.cfg
        self.t0_wall = time.time() + 0.5
        self.wall_of = lambda t: self.t0_wall + (t - c["start"]) / c["speed"]
        if c["live"] == "ui":                  # M2: the Electron app captures (mic in the renderer + audiotee in main) and pushes frames
            self.source = UiSource(self.asr.stdin, self.stop_source, sink=self._on_live_frame, on_warn=self.publish)
            self.wall_of = lambda t: (self.source.t0_wall or self.t0_wall) + t
        elif c["live"]:
            self.source = LiveCapture(self.asr.stdin, self.stop_source, on_pid=self.extra_pids.add, sink=self._on_live_frame, mode=c["capture"], run_dir=self.run_dir)
            self.wall_of = lambda t: (self.source.t0_wall or self.t0_wall) + t     # capture time 0 = first frame's wall clock
            if c["duration"]:
                threading.Timer(c["duration"], self.request_stop).start()
        else:
            self.source = FilePlayer(self.audio, c["start"], self.wall_of, self.asr.stdin, self.stop_source, mic=self.mic_audio)
        self.source.start()
        self.slides = None
        if self.has_video and not c["no_slides"] and not c["live"]:
            self.slides = SlideWatcher(c["input"], c["start"], c["end"], self.run_dir, self.wall_of, on_pid=self.extra_pids.add,
                                       on_slide=lambda t, words, lines: (self.store.add_slide(self.meeting_id, t, words, "\n".join(lines)),
                                                                          self.publish({"type": "slide", "t": t, "n_words": len(words)})))
            self.slides.start()

    def _on_live_frame(self, stream, t_end, samples):
        if stream == 0:
            self.sys_audio.append(samples)

    def _system_audio(self, a, b):
        """float32 of the system stream between absolute times a..b (file: decoded audio; live: captured frames)."""
        if self.audio is not None:
            return self.audio[int((a - self.cfg["start"]) * SR):int((b - self.cfg["start"]) * SR)]
        x = np.concatenate(self.sys_audio) if self.sys_audio else np.zeros(0, np.float32)
        return x[int(a * SR):int(b * SR)]

    # ---- 5. LLM client ---------------------------------------------------------------------------------------
    def _llm_send(self, o):
        with self.lock:
            if self.llm_quit and o["cmd"] in ("pause", "resume"):   # Lỗi 14: a newer meeting's guard must not write to a leaving worker
                return
            if o["cmd"] == "generate":
                self.llm_sent.add(o["id"])
            self.llm_w.write(json.dumps(o, ensure_ascii=False) + "\n"); self.llm_w.flush()

    def ask(self, question):
        """Việc 17: a question typed in the overlay. The meeting's own Gemma answers (queued with the live notes, and paused with
        them whenever the ASR lags, so the recording keeps priority); the answer streams as ask_delta events and is saved with
        the meeting (note kind "ask"), like the reference app keeps its chat with the meeting draft."""
        question = (question or "").strip()[:2000]
        if not question:
            return {"ok": False, "error": "empty question"}
        if self.state != "recording" or not self.llm:
            return {"ok": False, "error": "the notes model is still loading" if self.state in ("init", "recording") else "no meeting"}
        rows = sorted(((f["s"], f.get("speaker", ""), f["text"]) for f in list(self.finals) if not f.get("dropped_lang")), key=lambda r: r[0])
        p, n_rows, n_words = ask_ctx.prompt(PROMPTS["ask"], self.live_note, rows, question)
        aid = f"ask{self.n_ask}"; self.n_ask += 1
        self.asks[aid] = {"q": question, "t_sent": time.time(), "context_rows": n_rows, "context_words": n_words}
        self._llm_send({"cmd": "generate", "id": aid, "messages": [{"role": "user", "content": p}], "max_tokens": 600, "stream": True})
        return {"ok": True, "id": aid, "context_words": n_words}

    def _llm_reader(self):
        for line in self.llm_r:
            e = json.loads(line)
            if e["event"] == "delta":                 # Ask answer, token by token (not logged: the done event has the text)
                a = self.asks.get(e["id"])
                if a is not None and "t_first" not in a:
                    a["t_first"] = time.time()
                self.publish({"type": "ask_delta", "id": e["id"], "text": e["text"]})
                continue
            self.llm_events.write(line); self.llm_events.flush()
            if e["event"] == "done":                  # Lỗi 21: times come from the transcript, never from Gemma
                e["raw"] = e["text"]
                if e["id"].startswith("ask"):
                    e["text"] = stamps.keep_known(e["text"], [s for s, _ in self._stamp_rows()])
                elif e["id"].startswith("live"):           # Lỗi 21c: the MoM has per-part times from mom_c, no per-line stamps
                    e["text"], n, k = stamps.stamp(e["text"], self._stamp_rows())
                    self.stamped[e["id"][:4]] = {"stamped": n, "considered": k}
            if e["event"] == "done" and e["id"].startswith("mc"):     # Lỗi 21c map/reduce passes: _gen() waits for them
                self.llm_done[e["id"]] = e
                continue
            if e["event"] == "done" and e["id"].startswith("ask"):
                self.llm_done[e["id"]] = e
                a = self.asks.get(e["id"], {}); a.update(t_done=time.time(), stats=e["stats"])
                self.store.set_note(self.meeting_id, "ask", int(e["id"][3:]), json.dumps({"q": a.get("q"), "a": e["text"]}, ensure_ascii=False), e["stats"])
                self.publish({"type": "ask_done", "id": e["id"], "text": e["text"], "stats": e["stats"]})
                continue
            if e["event"] == "done":
                self.llm_done[e["id"]] = e
                kind, idx = ("mom", 0) if e["id"] == "mom" else ("live", int(e["id"][4:])) if e["id"].startswith("live") else ("part", int(e["id"][4:]))
                if kind == "live":
                    truncated = e["stats"].get("finish") == "length"
                    if truncated and self.sum_streak == 0:        # truncated notes lose their tail for good: keep the old notes,
                        self.live_truncated += 1                  # hand the blocks back to the next round (rnd v2 item 5) ...
                        self.tx.unsummarise(self.sum_rows); self.sum_streak = 1; keep = False
                    else:                                         # ... but a second truncation in a row is kept (a cut tail
                        if truncated:                             # beats blocks that never reach the notes / MoM)
                            self.live_truncated_kept += 1
                        self.live_note, self.sum_streak, keep = stamps.clean(e["raw"]), 0, True
                    self.sum_rows, self.sum_inflight, self.last_summary_end = [], False, time.time()
                if kind != "live" or keep:
                    self.store.set_note(self.meeting_id, kind, idx, e["text"], e["stats"])
                self.publish({"type": "note", "kind": kind, "idx": idx, "text": e["text"], "kept": kind != "live" or keep, "stats": e["stats"]})

    def _wait_llm(self, busy):
        """Wait while busy() is true. The reader thread ends when the Gemma worker's socket closes: after that no answer can come,
        so a worker that crashed mid-MoM raises here instead of leaving the meeting "finishing" forever (which also refused
        every Word export: midyd treats a finishing meeting as running)."""
        while busy():
            if not self.llm_reader_t.is_alive():
                raise RuntimeError("the local model (Gemma) stopped before answering, see llm.err")
            time.sleep(0.2)

    def _gen(self, tag, prompt, max_tokens):
        """One Gemma pass for mom_c, through this meeting's worker (pauses / external pause apply as for the live notes)."""
        self._llm_send({"cmd": "generate", "id": tag, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
        self._wait_llm(lambda: tag not in self.llm_done)
        e = self.llm_done.pop(tag)
        return e["text"], e["stats"]

    def _stamp_rows(self):
        return [(f["s"], f["text"]) for f in list(self.finals) if f["text"] and not f["dropped_lang"]]

    def _set_pause(self, p=None, ext=None):
        """Gemma pauses on this meeting's own guard (p) OR on the guard of a newer meeting recording meanwhile (ext, Lỗi 14)."""
        if p is not None and p != self.own_pause:
            self.own_pause = p
            if self.on_pause:
                self.on_pause(p)
        if ext is not None:
            self.ext_pause = ext
        eff = self.own_pause or self.ext_pause
        if self.llm and not self.llm_quit and eff != self.llm_paused:
            self.llm_paused = eff
            self._llm_send({"cmd": "pause" if eff else "resume"})
            self.llm_events.write(json.dumps({"event": "pause" if eff else "resume", "ext": self.ext_pause, "t": time.time()}) + "\n")

    def _chunk_bounds(self, k):
        w = self.cfg["chunk_min"] * 60
        return self.origin + k * w, self.origin + (k + 1) * w

    def _maybe_live_summary(self):
        """Việc 20: one live-notes round every LIVE_EVERY_S s at most (was the reference app rule: >= 30 new words, debounce 5 s)."""
        if not self.llm or self.new_final_words <= 0 or (self.sum_timer and self.sum_timer.is_alive()):
            return
        self.sum_timer = threading.Timer(max(0.0, self.next_live_at - time.time()), self._fire_live_summary)
        self.sum_timer.daemon = True; self.sum_timer.start()

    def _fire_live_summary(self, max_tokens=LIVE_MAX_TOKENS):
        if self.sum_inflight or self.state not in ("recording", "finishing"):     # "finishing": the end-of-meeting flush
            return                                                               # (was "recording" only => the flush never ran)
        gap = time.time() - self.last_summary_end
        if gap < SUM_MIN_GAP_S:
            self.sum_timer = threading.Timer(SUM_MIN_GAP_S - gap, self._fire_live_summary); self.sum_timer.daemon = True; self.sum_timer.start(); return
        with self.tx.lock:      # LLM eats ONLY pass-2 text (refined/frozen); pending sentences wait for the next round (rnd M1 item 3)
            rows = self.tx.take_unsummarised()          # per-block mark, any stream, any arrival order (rnd v2 item 4)
            if not rows:
                return
            self.sum_rows = rows
            tr = "\n".join(f"[{mmss(b['s'])}] {b['speaker'] or 'Speaker ?'}: {b['text']}" for b in rows)
        p = PROMPTS["live_summary"].replace("{{LANGUAGE}}", self._out_language()).replace("{{NOTES}}", self.live_note or "(empty)").replace("{{TRANSCRIPT}}", tr)
        self.sum_inflight, self.new_final_words = True, 0
        self.next_live_at = time.time() + LIVE_EVERY_S
        self._llm_send({"cmd": "generate", "id": f"live{self.n_live}", "messages": [{"role": "user", "content": p}], "max_tokens": max_tokens})
        self.n_live += 1

    def _chunk_done(self, k):
        """End of chunk k: rolling diarization on the chunk (CPU thread), relabel its groups, then the part notes."""
        def work():
            a, b = self._chunk_bounds(k)
            t = time.time(); changed = None
            try:
                if self.diar is None:
                    from diar_rolling import ChunkDiarizer
                    self.diar = ChunkDiarizer(threshold=self.cfg["diar_threshold"], num_speakers=self.cfg["num_speakers"])
                x = self._system_audio(max(a, self.cfg["start"]), b)
                if len(x) > 5 * SR:
                    j = self._diar_proc("chunk", x, self.cfg["diar_threshold"])   # heavy half in its own process (Lỗi 14)
                    local = {int(k): v for k, v in j["local"].items()}
                    cents = {int(k): (np.array(v) if v is not None else None) for k, v in j["cents"].items()}
                    dsegs, _ = self.diar.assign(local, cents, max(a, self.cfg["start"]))
                    rows = [f for f in self.finals if a <= f["s"] < b and f.get("stream", "system") != "mic"]
                    changed = self.diar.relabel(rows, dsegs)
                    for f in rows:
                        self.store.set_speaker(self.meeting_id, f["id"], f["speaker"])
                        if f["speaker"]:
                            self.tx.relabel_span(f["stream"], f["s"], f["e"], f["speaker"])
            except Exception as e:  # diarization must never take the notes down
                self.publish({"type": "warn", "where": "chunk_diar", "error": repr(e)})
            self.publish({"type": "chunk", "k": k, "diar_s": round(time.time() - t, 1), "labels_changed": changed,
                          "speakers_global": len(self.diar.centroids) if self.diar else None})
        th = threading.Thread(target=work, daemon=True); th.start(); return th

    def _diar_proc(self, mode, x, *args):
        """sherpa-onnx diarization in its own process: it holds the GIL for the whole run, and in the daemon that froze every
        thread, including a meeting recording meanwhile (Lỗi 14: 300 s of audio = 8.3 s frozen)."""
        npy = os.path.join(self.run_dir, f"diar_{mode}_{threading.get_ident()}.npy"); np.save(npy, np.asarray(x, np.float32))
        try:
            out = subprocess.run([PY, os.path.join(HERE, "diar_offline.py"), mode, npy, *map(str, args)], capture_output=True, text=True, env=ENV)
        finally:
            os.remove(npy)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[-300:])
        return json.loads(out.stdout)

    # ---- 6. main loop ----------------------------------------------------------------------------------------
    def _idle_watch(self):
        while not self.stop_sampler.is_set():
            idle = time.time() - self.last_speech_wall
            if idle >= IDLE_S and not self.idle_sent and self.state == "recording":
                self.idle_sent = True
                self.publish({"type": "idle", "silence_s": round(idle, 1)})      # the reference app: "No activity — end meeting?"
                if self.cfg["auto_end"]:
                    threading.Timer(IDLE_S, lambda: self.request_stop() if self.idle_sent else None).start()
            self.stop_sampler.wait(1.0)

    def _loop(self):
        events = open(os.path.join(self.run_dir, "events.jsonl"), "a")
        threads = []
        self.eof = None
        for raw in self.asr.stdout:
            line = raw.decode(); e = json.loads(line)
            events.write(line); events.flush()
            t = e["type"]
            if t == "partial":
                self.partials.append(e)
                if e["text"]:
                    self.last_speech_wall = time.time(); self.idle_sent = False
                if e["lag_s"] > LAG_NOTE_S and len(self.lag_peaks) < 50:
                    self.lag_peaks.append({"t_audio": round(e["t_audio"], 1), "lag_s": round(e["lag_s"], 2), "llm_active": any(i not in self.llm_done for i in self.llm_sent),
                                           "llm_paused": self.llm_paused, "summary_inflight": self.sum_inflight})
                if self.cfg["llm_policy"] == "guard":
                    if e["lag_s"] > 1.5:
                        self._set_pause(True)
                    elif e["lag_s"] < 0.5:
                        self._set_pause(False)
                if not e["final"]:
                    self.tx.listening(e["stream"], e["text"])
                self.publish({"type": "transcript", "source": e["stream"], "is_final": False, "t": e["t_audio"], "text": e["stable"] or e["text"], "lag_s": e["lag_s"]})
            elif t == "level":
                self.levels[e["stream"]] = {"t": e["t_audio"], "rms_dbfs": e["rms_dbfs"], "peak": e["peak"], "capture_rate": e.get("capture_rate")}
                self.rates.setdefault(e["stream"], []).append(e.get("capture_rate", 1.0))
                if self.cfg["live"] and e.get("capture_rate", 1.0) < 0.98:   # rnd rule: a stream must prove it delivers its samples
                    self.publish({"type": "warn", "where": "capture_rate", "stream": e["stream"], "capture_rate": e["capture_rate"]})
            elif t == "seg_start":
                self.last_speech_wall = time.time(); self.idle_sent = False
                if self.cfg["llm_policy"] == "silence":
                    self._set_pause(True)
            elif t == "seg_end":
                self.segs.append(e)
                if self.cfg["llm_policy"] == "silence":
                    self._set_pause(False)
                text = "" if e.get("p1_dropped") or e.get("p1_leak") else e["p1_text"]
                spk = "You" if e["stream"] == "mic" else e.get("speaker", "")          # the reference app: the mic is "You" (CEO C3)
                b = self.tx.sentence_final(e["stream"], e["s"], e["e"], e["speech_end"], text, speaker=spk)
                self.blocks_fd.write(json.dumps({"ev": "sentence_final", "t_emit": time.time(), **self.tx.view(b)}, ensure_ascii=False) + "\n"); self.blocks_fd.flush()
                self.new_final_words += len(words(text)); self._maybe_live_summary()
            elif t == "final":
                e["id"] += self.seq_base                     # keep seq unique across a resume
                if e["stream"] == "mic":
                    e["speaker"] = "You"
                self.finals.append(e)
                self.store.add_segment(self.meeting_id, e)
                # no `transcript is_final:true` per group: in window mode the refined text arrives through transcript_window
                # (a v2 client would otherwise addEntry it twice — rnd M1 item 1c); groups stay queryable via `transcript` from SQLite
                ids = self.tx.refine(e["stream"], e["s"], e["e"], e["text"], speaker=e["speaker"], dropped=e["dropped_lang"])
                for bid in ids:
                    self.blocks_fd.write(json.dumps({"ev": "refined", "group": e["id"], "t_emit": time.time(), **self.tx.view(self.tx.blocks[bid])}, ensure_ascii=False) + "\n")
                self.blocks_fd.flush()
                while self.llm and e["s"] >= self._chunk_bounds(self.next_chunk)[1]:
                    threads.append(self._chunk_done(self.next_chunk)); self.next_chunk += 1
            elif t == "eof":
                self.eof = e; break
        self.t_eof = time.time()
        self._set_pause(False)
        for th in threads:
            th.join()

    # ---- 7. finish -------------------------------------------------------------------------------------------
    def _finish(self):
        c = self.cfg
        self.state = "finishing"; self.store.set_status(self.meeting_id, "finishing")
        self.mom_stats = None
        if not self.cfg["no_llm"] and not self.llm:
            self.error = "the local model (Gemma) did not start, see llm.err"
        if self.llm:
            th = self._chunk_done(self.next_chunk); th.join()
            self._wait_llm(lambda: any(p not in self.llm_done for p in self.part_ids))
            if self.sum_timer:
                self.sum_timer.cancel()
            self._wait_llm(lambda: self.sum_inflight)
            for cap in (LIVE_MAX_TOKENS, 2 * LIVE_MAX_TOKENS):             # flush every refined block not yet summarised
                self.last_summary_end = 0.0; self._fire_live_summary(cap)  # (second pass, double cap, only if the first was truncated)
                self._wait_llm(lambda: self.sum_inflight)
                if self.tx.unsummarised_left() == 0:
                    break
            # Lỗi 21c (option C): the MoM reads the transcript in 600 s parts; the live notes are for viewing only
            self.mom_language = self._out_language()
            mom, parts, self.mom_c_info = mom_c.build_mom(list(self.finals), self.mom_language, self._gen)
            open(os.path.join(self.run_dir, "notes_parts.md"), "w").write(mom_c.parts_text(parts))
            if mom:
                open(os.path.join(self.run_dir, "mom.md"), "w").write(mom)
                i = self.mom_c_info
                self.mom_stats = {"wall_s": i["gen_s"], "gen_tokens": i["gen_tokens"], "parts": i["parts"], "map_cut": i["map_cut"], "reduce_cut": i["reduce_cut"]}
                self.store.set_note(self.meeting_id, "mom", 0, mom, self.mom_stats)
                self.publish({"type": "note", "kind": "mom", "idx": 0, "text": mom, "kept": True, "stats": self.mom_stats})
                for w in i["warn"]:
                    self.publish({"type": "warn", "where": "mom", "error": w})
                name = (self.store.meeting(self.meeting_id) or {}).get("name") or ""
                if mom.startswith("# ") and re.match(r"(Untitled Note|meeting \d)", name):   # the reference app generate-note-title = our MoM H1
                    self.store.set_name(self.meeting_id, re.sub(r"^#\s*", "", mom.split("\n")[0]).strip()[:120])  # (was in the app until Lỗi 14)
            with self.lock:
                self.llm_quit = True              # no pause/resume to a worker that is leaving
            self._llm_send({"cmd": "quit"})
            try:
                self.llm.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.llm.kill()
        self.t_mom_done = time.time()
        # whole-meeting offline diarization on the system stream (design 1.5a step 2) -> transcript.md. Own process (Lỗi 14):
        # sherpa-onnx holds the GIL for the whole run, which froze a meeting recording meanwhile (diar_offline.py).
        t = time.time()
        full = self._system_audio(self.origin if self.audio is None else c["start"], 1e9)
        off0 = self.origin if self.audio is None else c["start"]
        self.dsegs = []
        if len(full) > SR:
            try:
                self.dsegs = [(off0 + s0, off0 + e0, sp) for s0, e0, sp in self._diar_proc("whole", full, c["num_speakers"], c["diar_threshold"])]
            except RuntimeError as e:              # speakers stay the online ones; never silent
                self.publish({"type": "warn", "where": "offline_diar", "error": str(e)})
        self.diar_s = time.time() - t
        self.changed = 0
        with open(os.path.join(self.run_dir, "transcript.md"), "w") as f:
            for g in self.finals:
                ov = {}
                for s0, e0, sp in self.dsegs:
                    o = min(e0, g["e"]) - max(s0, g["s"])
                    if o > 0:
                        ov[sp] = ov.get(sp, 0) + o
                off = f"Speaker {max(ov, key=ov.get) + 1}" if ov else ""
                g["speaker_offline"] = off
                self.changed += bool(off) and off != g["speaker"]
                f.write(f"[{mmss(g['s'])}] {off or g['speaker'] or 'Speaker ?'}: {g['text']}\n" if not g["dropped_lang"]
                        else f"[{mmss(g['s'])}] [dropped: ~{int(g['e'] - g['s'])} s in another language]\n")
        with self.tx.lock:                                                     # server-side final state, for client_check.py
            json.dump([self.tx.view(b) for b in self.tx.blocks], open(os.path.join(self.run_dir, "blocks_final.json"), "w"), ensure_ascii=False)
        with open(os.path.join(self.run_dir, "transcript_lines.md"), "w") as f:   # the reference app display rule (30 words + end punctuation)
            for l in self.tx.lines():
                f.write(f"{l['speaker'] or 'Speaker ?'}: {l['text']}\n")
        self.stop_sampler.set(); time.sleep(1.2)
        if self.slides:
            self.slides.join(timeout=5)
        try:
            self.asr.wait(timeout=30)
            if self.llm:
                self.llm.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.cleanup()
        self.store.set_status(self.meeting_id, "error: " + self.error if getattr(self, "error", None) else "done")
        self.state = "done"
        self.publish({"type": "done", "meeting_id": self.meeting_id})

    # ---- 8. stats --------------------------------------------------------------------------------------------
    def stats(self):
        c, wall_of = self.cfg, self.wall_of
        finals, partials, segs = self.finals, self.partials, self.segs
        new = [f for f in finals if "t_emit" in f]          # resumed rows have no timing
        lat_partial = [p["t_emit"] - wall_of(p["t_audio"]) for p in partials if not p["final"]]
        lat_final = [f["t_emit"] - wall_of(f["speech_end"]) for f in new]
        lat_first = [f["t_emit"] - wall_of(f["first_end"]) for f in new]
        lat_seg_final = [p["t_emit"] - wall_of(p["speech_end"]) for p in partials if p["final"] and "speech_end" in p]
        rss_peak, sw = {}, []
        for l in open(os.path.join(self.run_dir, "samples.jsonl")):
            r = json.loads(l)
            for k, v in r["rss_gb"].items():
                rss_peak[k] = max(rss_peak.get(k, 0), v)
            if "swap_used_gb" in r:
                sw.append(r)
        llm_stats = [json.loads(l) for l in open(os.path.join(self.run_dir, "llm_events.jsonl")) if '"done"' in l] if self.llm else []
        pauses = [l for l in open(os.path.join(self.run_dir, "llm_events.jsonl")) if '"pause"' in l] if self.llm else []
        bye = [json.loads(l) for l in open(os.path.join(self.run_dir, "llm_events.jsonl")) if '"bye"' in l] if self.llm else []
        return {
            "run": c["name"], "meeting_id": self.meeting_id, "input": os.path.basename(c["input"] or "live"), "window_s": [c["start"], c["end"]],
            "audio_s": round(len(self.audio) / SR, 1) if self.audio is not None else round(getattr(self.source, "t_last", 0), 1),
            "speed": c["speed"], "t0_wall": self.t0_wall, "language": c["language"], "mom_language": getattr(self, "mom_language", None), "llm_policy": c["llm_policy"] if self.llm else "no-llm",
            "sandbox": os.environ.get("MIDY_SANDBOX") == "1", "ready": self.ready, "resumed_groups": len(finals) - len(new),
            "files_mode": oct(os.stat(os.path.join(self.run_dir, "events.jsonl")).st_mode & 0o777),
            "wall_total_s": round(self.t_mom_done - self.t0_wall, 1),
            "warm_workers": getattr(self, "warm", None),
            "wall_after_audio_end_s": {"llm_tail": round(self.t_mom_done - self.t_eof, 1), "offline_diarization": round(self.diar_s, 1)},
            "asr": {**{k: v for k, v in (self.eof or {}).items() if k != "type"}, "n_partials": len(lat_partial), "n_finals": len(finals),
                    "partial_latency_s": {"p50": pct(lat_partial, 50), "p90": pct(lat_partial, 90), "max": pct(lat_partial, 100)},
                    "word_to_screen_estimate_s": {"p50": pct([x + 1.0 for x in lat_partial], 50), "worst": pct([x + 2.0 for x in lat_partial], 90),
                                                  "note": "partial latency + wait for the 2 s chunk (mean 1 s, worst 2 s); ESTIMATE"},
                    "utterance_end_latency_from_speech_end_s": {"p50": pct(lat_seg_final, 50), "p90": pct(lat_seg_final, 90)},
                    "final_latency_last_sentence_s": {"p50": pct(lat_final, 50), "p90": pct(lat_final, 90), "max": pct(lat_final, 100)},
                    "final_latency_first_sentence_s": {"p50": pct(lat_first, 50), "p90": pct(lat_first, 90)},
                    "pass2_compute_s": {"p50": pct([f["compute_s"] for f in new], 50), "p90": pct([f["compute_s"] for f in new], 90)},
                    "queue_lag_s": {"p50": pct([p["lag_s"] for p in partials], 50), "p90": pct([p["lag_s"] for p in partials], 90), "max": pct([p["lag_s"] for p in partials], 100)},
                    "lag_peaks_over_%.1fs" % LAG_NOTE_S: sorted(self.lag_peaks, key=lambda x: -x["lag_s"])[:10],
                    "partials_dropped_lang": sum(p.get("dropped_lang", False) for p in partials),
                    "groups_dropped_lang": sum(f["dropped_lang"] for f in finals), "seconds_dropped_lang": round(sum(f["e"] - f["s"] for f in finals if f["dropped_lang"]), 1),
                    "groups_ctx_leak": sum(f.get("ctx_leak", 0) for f in finals), "groups_ctx_leak_both": sum(f.get("ctx_leak_both", False) for f in new),
                    "groups_with_ctx": sum(bool(f["ctx"]) for f in finals), "glossary_corrections": sum(f.get("n_corrections", 0) for f in finals),
                    "words_final": sum(len(f["text"].split()) for f in finals if not f["dropped_lang"]),
                    "cjk_chars_kept": sum(len(CJK.findall(f["text"])) for f in finals if not f["dropped_lang"]),
                    "segments": {"system": sum(s["stream"] != "mic" for s in segs), "mic": sum(s["stream"] == "mic" for s in segs),
                                 "mic_overlapping_system": sum(1 for m in segs if m["stream"] == "mic" and any(x["stream"] != "mic" and min(x["e"], m["e"]) > max(x["s"], m["s"]) for x in segs))},
                    "speakers_online": self.eof and self.eof.get("n_speakers_online"), "speakers_rolling": len(self.diar.centroids) if self.diar else None,
                    "speakers_offline": len({d[2] for d in self.dsegs}), "groups_label_changed_by_offline": self.changed,
                    "embed_s_p50": pct([s["embed_s"] for s in segs if s["embed_s"]], 50)},
            "blocks": {**{k: v for k, v in self.tx.stats.items() if k != "word_change_ratios"},
                       "rewritten_share": round(self.tx.stats["rewritten"] / max(1, self.tx.stats["refined"]), 3),
                       "rewritten_norm_share": round(self.tx.stats["rewritten_norm"] / max(1, self.tx.stats["refined"]), 3),
                       "rewritten_2plus_words_share": round(self.tx.stats["rewritten_2plus_words"] / max(1, self.tx.stats["refined"]), 3),
                       "word_change_ratio": {"p50": pct(self.tx.stats["word_change_ratios"], 50), "mean": round(float(np.mean(self.tx.stats["word_change_ratios"])), 3) if self.tx.stats["word_change_ratios"] else None},
                       "sentence_final_latency_s": {"p50": pct(lat_seg_final, 50), "p90": pct(lat_seg_final, 90), "max": pct(lat_seg_final, 100)},
                       "lines_display_rule": len(self.tx.lines())},
            "asks": [{"id": k, "context_words": a["context_words"], "first_token_s": round(a["t_first"] - a["t_sent"], 2) if "t_first" in a else None,
                      "answer_s": round(a["t_done"] - a["t_sent"], 2) if "t_done" in a else None, "prompt_tokens": a.get("stats", {}).get("prompt_tokens"),
                      "gen_tokens": a.get("stats", {}).get("gen_tokens"), "paused_s": a.get("stats", {}).get("paused_s")} for k, a in self.asks.items()],
            "live_summary": {"rule": "the reference app 5s/30w/10s, output <= 12 lines / %d tokens" % LIVE_MAX_TOKENS, "n": len([s for s in llm_stats if s["id"].startswith("live")]),
                             "truncated_discarded": self.live_truncated, "truncated_kept": self.live_truncated_kept, "finish_length": sum(s["stats"].get("finish") == "length" for s in llm_stats if s["id"].startswith("live")),
                             "blocks_refined_with_text": sum(1 for b in self.tx.blocks if b["state"] in ("refined", "frozen") and b["text"] and not b.get("dropped")),
                             "blocks_summarised": self.tx.stats["summarised"], "blocks_refined_not_summarised": self.tx.unsummarised_left(),
                             "per_stream": {src: {"refined": sum(1 for b in self.tx.blocks if b["source"] == src and b["state"] in ("refined", "frozen") and b["text"] and not b.get("dropped")),
                                                  "summarised": sum(1 for b in self.tx.blocks if b["source"] == src and b.get("summarised"))} for src in ("system", "mic")},
                             "per_summary": [{"id": s["id"], **s["stats"]} for s in llm_stats if s["id"].startswith("live")][:50],
                             "llm_busy_s": round(sum(s["stats"]["wall_s"] - s["stats"]["paused_s"] for s in llm_stats if s["id"].startswith("live")), 1)} if self.llm else None,
            "stamps": self.stamped,
            "mom_c": getattr(self, "mom_c_info", None),
            "slides": self.slides.stats if self.slides else None,
            "llm": {"n_generations": len(llm_stats), "per_generation": [{"id": s["id"], **s["stats"]} for s in llm_stats],
                    "gen_tps_median": pct([s["stats"]["mlx_gen_tps"] for s in llm_stats], 50), "prompt_tps_median": pct([s["stats"]["mlx_prompt_tps"] for s in llm_stats], 50),
                    "pause_events": len(pauses), "bye": bye[0] if bye else None} if self.llm else None,
            "memory": {"rss_peak_gb": rss_peak, "swap_used_gb_baseline": self.baseline["swap_used_gb"], "swap_used_gb_max": max([s["swap_used_gb"] for s in sw] or [0]),
                       "pageouts_delta": (sw[-1]["pageouts"] - self.baseline["pageouts"]) if sw else None,
                       "memorystatus_level_min": min([s["memorystatus_level"] for s in sw] or [None]), "memorystatus_level_baseline": self.baseline["memorystatus_level"],
                       "loadavg_1m_max": max([s.get("loadavg_1m", 0) for s in sw] or [None])},
            "source_t0_wall": getattr(self.source, "t0_wall", None), "silence_inserted_s": getattr(self.source, "silence_inserted", None),
            "capture": {"input_volume_start_end": [self.baseline.get("input_volume"), input_volume()],
                        "capture_rate": {k: {"n": len(v), "min": round(min(v), 3), "p5": pct(v, 5), "p50": pct(v, 50), "below_0.98": sum(x < 0.98 for x in v)} for k, v in self.rates.items()}},
            "network": {"lsof_i_lines_total": len(self.net_lines), "lsof_i_sample": self.net_lines[:5],
                        "worker_selftest": {"asr": self.ready["asr"].get("net_selftest"), "llm": self.ready.get("llm", {}).get("net_selftest")}},
        }

    # ---- run -------------------------------------------------------------------------------------------------
    def request_stop(self):
        self.stop_source.set()

    def run(self):
        import atexit
        atexit.register(self.cleanup)
        self._prepare(); self._spawn()
        self.stop_sampler = threading.Event()
        threading.Thread(target=self._sampler, daemon=True).start()
        threading.Thread(target=self._idle_watch, daemon=True).start()
        self.state = "recording"
        self.next_live_at = time.time() + LIVE_EVERY_S          # first live notes after one period of speech
        self._start_source()
        self._loop()
        self._finish()
        st = self.stats()
        json.dump(st, open(os.path.join(self.run_dir, "stats.json"), "w"), indent=1)
        return st
