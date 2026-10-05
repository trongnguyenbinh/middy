"""Slide reader (design 1.5), driven by a video file in real time to stand in for screen capture.

1 frame/s at 320x180 grey; >5 % of pixels changed by >20 levels = "slide changed"; take the first frame
that is still again (<1 %); frames >= 3 s apart. Then extract the full-res frame and OCR it with Apple Vision
(tools/ocr, on device). Appends {"t", "words", "n_lines", ...} to <run-dir>/slides.jsonl and OCR lines to ocr.tsv.
"""
import json
import os
import subprocess
import threading
import time

import numpy as np

from glossary import slide_terms

W, H = 320, 180
CHANGE, STILL, MIN_GAP = 0.05, 0.01, 3
OCR_BIN = os.path.join(os.path.dirname(__file__), "..", "tools", "ocr")


class SlideWatcher(threading.Thread):
    def __init__(self, video, start, end, run_dir, wall_of, on_pid=None, on_slide=None):
        super().__init__(daemon=True)
        self.video, self.t_start, self.t_end, self.run_dir, self.wall_of, self.on_pid, self.on_slide = video, start, end, run_dir, wall_of, on_pid, on_slide  # not .start/.end: Thread.start()
        self.stats = {"frames": 0, "changes": 0, "slides": 0, "ocr_lines": 0, "detect_to_ocr_s": []}
        os.makedirs(os.path.join(run_dir, "frames"), exist_ok=True)

    def run(self):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-hwaccel", "videotoolbox", "-ss", str(self.t_start)]
        if self.t_end:
            cmd += ["-to", str(self.t_end)]
        cmd += ["-i", self.video, "-vf", f"fps=1,scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        if self.on_pid:
            self.on_pid(p.pid)
        prev, want_still, last_t = None, True, -MIN_GAP
        i = 0
        while (b := p.stdout.read(W * H)) and len(b) == W * H:
            t = self.t_start + i
            time.sleep(max(0.0, self.wall_of(t) - time.time()))     # pace like a live 1 fps capture
            fr = np.frombuffer(b, np.uint8).astype(np.int16)
            frac = float(np.mean(np.abs(fr - prev) > 20)) if prev is not None else 0.0
            prev = fr
            self.stats["frames"] += 1
            if frac > CHANGE:
                want_still = True; self.stats["changes"] += 1
            elif want_still and frac < STILL and t - last_t >= MIN_GAP:
                want_still, last_t = False, t
                self.capture(t)
            i += 1
        p.wait()

    def capture(self, t):
        t_det = time.time()
        png = os.path.join(self.run_dir, "frames", f"k_{int(t):05d}.png")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(t), "-i", self.video, "-frames:v", "1", png])
        out = subprocess.run([OCR_BIN, png], capture_output=True, text=True).stdout
        lines = [l.split("\t", 1)[1] for l in out.splitlines() if "\t" in l]
        words = slide_terms(lines)
        with open(os.path.join(self.run_dir, "ocr.tsv"), "a") as f:
            for l in lines:
                f.write(f"{int(t)}\t{l}\n")
        with open(os.path.join(self.run_dir, "slides.jsonl"), "a") as f:
            f.write(json.dumps({"t": t, "words": words, "n_lines": len(lines), "t_wall": time.time(),
                                "latency_s": round(time.time() - t_det, 2)}) + "\n")
        self.stats["slides"] += 1; self.stats["ocr_lines"] += len(lines)
        if self.on_slide:
            self.on_slide(t, words, lines)
        self.stats["detect_to_ocr_s"].append(round(time.time() - t_det, 2))
