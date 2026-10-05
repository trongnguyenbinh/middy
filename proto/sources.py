"""Audio sources feeding the ASR worker with the frame protocol of common.py.

FilePlayer   a decoded recording, played at wall-clock speed (measurement); system = the recording, mic = silence
LiveCapture  system audio through **audiotee** (MIT, built from the pinned upstream
             source in tools/audiotee-src) as raw PCM 16 kHz mono s16le, 250 ms chunks; the microphone through
             ffmpeg/avfoundation (the Electron shell of M2 takes over).
             Fallback: --capture audiocap uses tools/audiocap (own Swift helper doing both).
"""
import os
import subprocess
import threading
import time

import numpy as np

from common import SR, FRAME_S, HDR, write_frame, EOF_HDR, STREAMS

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIOTEE = os.path.join(HERE, "..", "tools", "audiotee-src", ".build", "release", "audiotee")
AUDIOCAP = os.path.join(HERE, "..", "tools", "audiocap")
BYTES_PER_FRAME = int(FRAME_S * SR) * 2          # s16le mono


class FilePlayer(threading.Thread):
    def __init__(self, audio, start, wall_of, out_fd, stop_event, mic=None):
        super().__init__(daemon=True)
        self.audio, self.start_s, self.wall_of, self.fd, self.stop = audio, start, wall_of, out_fd, stop_event
        self.mic = mic                         # optional second track for stream 1 (two-stream regression); None = silence
        self.t_last = start

    def run(self):
        n = int(FRAME_S * SR)
        zeros = np.zeros(n, np.float32)
        for i in range(0, len(self.audio), n):
            if self.stop.is_set():
                break
            x = self.audio[i:i + n]
            t_end = self.start_s + (i + len(x)) / SR
            time.sleep(max(0.0, self.wall_of(t_end) - time.time()))
            write_frame(self.fd, 0, t_end, x)                 # system = the recording
            m = self.mic[i:i + len(x)] if self.mic is not None else zeros[:len(x)]   # mic = silence unless a second track is given
            write_frame(self.fd, 1, t_end, np.concatenate([m, zeros[:len(x) - len(m)]]) if len(m) < len(x) else m)
            self.fd.flush()
            self.t_last = t_end
        self.fd.write(EOF_HDR); self.fd.flush(); self.fd.close()


class LiveCapture(threading.Thread):
    """Two capture processes -> one frame stream. t_end of a frame = seconds of that stream captured so far."""
    def __init__(self, out_fd, stop_event, on_pid=None, sink=None, mode="audiotee", mic_device=":0", run_dir="/tmp"):
        """mode: audiotee = system via audiotee + mic via tools/audiocap --mic-only (AVAudioEngine);
                 ffmpeg   = system via audiotee + mic via ffmpeg/avfoundation (kept for comparison: loses ~75-85 % of samples
                            while another app holds the mic with voice processing, measured 25/09);
                 audiocap = both streams via tools/audiocap."""
        super().__init__(daemon=True)
        self.fd, self.stop, self.on_pid, self.sink, self.mode, self.mic_device, self.run_dir = out_fd, stop_event, on_pid, sink, mode, mic_device, run_dir
        self.t_last = 0.0
        self.t0_wall = None            # wall clock of capture time 0 (set by the first frame)
        self.lock = threading.Lock()
        self.procs = []
        self.errors = {}

    def _emit(self, stream, t_end, samples):
        with self.lock:
            if self.t0_wall is None:
                self.t0_wall = time.time() - t_end
            write_frame(self.fd, stream, t_end, samples); self.fd.flush()
            self.t_last = max(self.t_last, t_end)
        if self.sink:
            self.sink(stream, t_end, samples)

    def _pcm_reader(self, proc, stream):
        n = 0
        while not self.stop.is_set():
            b = proc.stdout.read(BYTES_PER_FRAME)
            if len(b) < BYTES_PER_FRAME:
                break
            n += BYTES_PER_FRAME // 2
            self._emit(stream, n / SR, np.frombuffer(b, np.int16).astype(np.float32) / 32768.0)

    def _protocol_reader(self, proc):
        while not self.stop.is_set():
            h = proc.stdout.read(HDR.size)
            if len(h) < HDR.size:
                break
            stream, t_end, _t_sent, n = HDR.unpack(h)
            self._emit(stream, t_end, np.frombuffer(proc.stdout.read(4 * n), np.float32))

    def run(self):
        threads = []
        if self.mode in ("audiotee", "ffmpeg"):
            sys_p = subprocess.Popen([AUDIOTEE, "--sample-rate", str(SR), "--chunk-duration", str(FRAME_S)],
                                     stdout=subprocess.PIPE, stderr=open(os.path.join(self.run_dir, "audiotee.err"), "w"))
            if self.mode == "ffmpeg":
                mic_p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "warning", "-f", "avfoundation", "-i", self.mic_device,   # warning: shows dropped frames
                                          "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=open(os.path.join(self.run_dir, "mic.err"), "w"))
                mic_t = threading.Thread(target=self._pcm_reader, args=(mic_p, 1), daemon=True)
            else:
                mic_p = subprocess.Popen([AUDIOCAP, "--mic-only"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=open(os.path.join(self.run_dir, "mic.err"), "w"))
                mic_t = threading.Thread(target=self._protocol_reader, args=(mic_p,), daemon=True)
            self.procs = [sys_p, mic_p]
            threads = [threading.Thread(target=self._pcm_reader, args=(sys_p, 0), daemon=True), mic_t]
        else:
            cap = subprocess.Popen([AUDIOCAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.path.join(self.run_dir, "audiocap.err"), "w"))   # helper exits when stdin closes
            self.procs = [cap]
            threads = [threading.Thread(target=self._protocol_reader, args=(cap,), daemon=True)]
        for p in self.procs:
            if self.on_pid:
                self.on_pid(p.pid)
        for t in threads:
            t.start()
        while not self.stop.is_set() and any(t.is_alive() for t in threads):
            self.stop.wait(0.5)
        for p in self.procs:
            p.terminate()
        for t in threads:
            t.join(timeout=2)
        with self.lock:
            self.fd.write(EOF_HDR); self.fd.flush(); self.fd.close()


class UiSource(threading.Thread):
    """Frames pushed by the UI (M2): the renderer captures the mic (getUserMedia + AEC3) and the main process runs audiotee;
    both arrive here as s16le 16 kHz chunks over the daemon socket. t_end of a stream = seconds of that stream received so far."""
    DRIFT_S = 2.0                     # a stream this far behind the wall clock gets silence inserted + a warning (rnd M2 item 8)

    def __init__(self, out_fd, stop_event, sink=None, on_warn=None):
        super().__init__(daemon=True)
        self.fd, self.stop, self.sink, self.on_warn = out_fd, stop_event, sink, on_warn
        self.silence_inserted = {0: 0.0, 1: 0.0}
        self.drift_warned = {0: False, 1: False}
        self.last_tcap = {0: None, 1: None}      # latest capture timestamp received per stream (drift reference, rnd M2 v2 item 5)
        self.t_last, self.t0_wall = 0.0, None
        self.t_end = {0: 0.0, 1: 0.0}
        self.n_chunks = {0: 0, 1: 0}
        self.lock = threading.Lock()

    def feed(self, stream, pcm_s16le, t_capture=None):
        """t_capture = wall clock at the END of the chunk when it was captured (the UI pre-buffers audio while the models load,
        so the first chunk may arrive seconds after it was captured: t0_wall is anchored on capture time, not arrival)."""
        x = np.frombuffer(pcm_s16le, np.int16).astype(np.float32) / 32768.0
        if not len(x) or self.stop.is_set():
            return
        with self.lock:
            if self.t0_wall is None:
                self.t0_wall = (t_capture - len(x) / SR) if t_capture else time.time()
            self.t_end[stream] += len(x) / SR
            self.n_chunks[stream] += 1
            if t_capture:
                self.last_tcap[stream] = t_capture
            write_frame(self.fd, stream, self.t_end[stream], x); self.fd.flush()
            self.t_last = max(self.t_last, self.t_end[stream])
        if self.sink:
            self.sink(stream, self.t_end[stream], x)

    def check_drift(self):
        """Called every second by the session: a stream whose received seconds lag the wall clock by > DRIFT_S (its device
        stopped or dropped samples) gets zeros inserted so both streams keep one time axis; the UI is warned once."""
        if self.t0_wall is None or self.stop.is_set():
            return
        # reference = the newest CAPTURE time seen on either stream (chunks carry it), so a pre-buffer being flushed is never
        # mistaken for a stalled stream; a stream that really died still lags because the other one keeps pushing the
        # reference forward. Wall clock only when the sender gives no timestamps (tests / older clients).
        tc = [t for t in self.last_tcap.values() if t]
        wall = (max(tc) if tc else time.time()) - self.t0_wall
        with self.lock:
            for stream in (0, 1):
                lag = wall - self.t_end[stream]
                if lag > self.DRIFT_S:
                    n = int((lag - 0.5) * SR)
                    write_frame(self.fd, stream, self.t_end[stream] + n / SR, np.zeros(n, np.float32)); self.fd.flush()
                    self.t_end[stream] += n / SR; self.silence_inserted[stream] += n / SR
                    if not self.drift_warned[stream] and self.on_warn:
                        self.drift_warned[stream] = True
                        self.on_warn({"type": "warn", "where": "stream_drift", "stream": STREAMS[stream], "lag_s": round(lag, 1)})
                elif lag < 1.0 and self.drift_warned[stream]:
                    self.drift_warned[stream] = False
                    if self.on_warn:
                        self.on_warn({"type": "warn", "where": "stream_drift_end", "stream": STREAMS[stream]})

    def run(self):
        while not self.stop.wait(1.0):
            self.check_drift()
        with self.lock:
            self.fd.write(EOF_HDR); self.fd.flush(); self.fd.close()
