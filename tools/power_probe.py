"""Việc 19 measurement (no code change to Middy): energy / CPU / GPU of Middy's daemon tree in each state, without sudo.
  tools/power_probe.py            ~16 min; test daemon on run/l19.db, public B1 audio, no window, no speaker
Per process of the tree: CPU energy (J, proc_pid_rusage ri_energy_nj), CPU time (ps), GPU time (s, ioreg AGXDeviceUserClient
accumulatedGPUTime). `top -stats power` is NOT used: on this Mac its POWER column equals %CPU and leaves the GPU out.
GPU energy in joules needs powermetrics (sudo) -> not measured; GPU seconds are reported instead.
"""
import base64
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time

import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, "..")
PY = os.path.join(ROOT, ".venv", "bin", "python")
run = os.path.join(ROOT, "run"); sock_path = os.path.join(run, "l19.sock"); db = os.path.join(run, "l19.db")
for f in (db, db + "-wal", db + "-shm"):
    if os.path.exists(f):
        os.remove(f)
IDLE_S = int(os.environ.get("IDLE_S", 600))


from procstat import cputime, cpu_energy_j, gpu_seconds  # noqa: E402
from procstat import gpu_util as gpu  # noqa: E402


def tree(root):
    rows = [l.split(None, 4) for l in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,rss=,command="], capture_output=True, text=True).stdout.splitlines()]
    kids = {}
    for p in rows:
        kids.setdefault(p[1], []).append(p)
    out, todo = {}, [str(root)]
    while todo:
        pid = todo.pop()
        for k in kids.get(pid, []):
            todo.append(k[0])
        for p in rows:
            if p[0] == pid:
                name = "daemon" if "midyd.py" in p[4] else "asr" if "asr_worker" in p[4] else "llm" if "llm_worker" in p[4] else "diar" if "diar_offline" in p[4] else "other"
                out[pid] = {"name": name, "cpu_s": cputime(p[2]), "rss_gb": int(p[3]) / 1e6}
    return out


def snap(root):
    g = gpu_seconds()
    return {pid: {"name": i["name"], "cpu_s": i["cpu_s"], "cpu_j": cpu_energy_j(pid) or 0.0, "gpu_s": g.get(pid, 0.0), "rss_gb": i["rss_gb"]}
            for pid, i in tree(root).items()}


class Phase:
    """Per-process totals over a phase; processes that exit keep their last value, processes that appear count from 0."""
    def __init__(self, root):
        self.root, self.t0, self.first, self.last, self.util, self.rss = root, time.time(), snap(root), {}, [], {}
        self.last = {k: dict(v) for k, v in self.first.items()}

    def tick(self):
        for pid, v in snap(self.root).items():     # cumulative counters: keep the max (an exited worker is a zombie reading 0)
            old = self.last.get(pid)
            self.last[pid] = v if not old else {**v, **{k: max(old[k], v[k]) for k in ("cpu_s", "cpu_j", "gpu_s")}}
            self.rss[v["name"]] = max(self.rss.get(v["name"], 0), v["rss_gb"])
        self.util.append(gpu())

    def result(self, gpu_base):
        mins = (time.time() - self.t0) / 60
        by = {}
        for pid, v in self.last.items():
            f = self.first.get(pid, {"cpu_s": 0, "cpu_j": 0, "gpu_s": 0})
            b = by.setdefault(v["name"], {"cpu_s": 0.0, "cpu_j": 0.0, "gpu_s": 0.0})
            for k in b:
                b[k] += max(0.0, v[k] - f[k])
        tot = {k: round(sum(b[k] for b in by.values()), 2) for k in ("cpu_s", "cpu_j", "gpu_s")}
        u = [x for x in self.util if x is not None]
        return {"minutes": round(mins, 2), **tot, "cpu_w_avg": round(tot["cpu_j"] / (mins * 60), 3), "gpu_s_per_min": round(tot["gpu_s"] / mins, 2),
                "gpu_sys_util_mean": round(statistics.mean(u), 1) if u else None, "gpu_sys_over_base": round(statistics.mean(u) - gpu_base, 1) if u else None,
                "by_process": {n: {k: round(x, 2) for k, x in b.items()} for n, b in by.items()}, "rss_gb_max": {n: round(x, 2) for n, x in self.rss.items()}}


out = {}
# 0. baseline: the machine without any Middy process
gb = [gpu() for _ in range(12) if not time.sleep(2.5)]
gpu_base = statistics.mean(gb); out["0_baseline_gpu_sys_mean"] = round(gpu_base, 1)

# 1. load: daemon start -> both workers warm (models in page cache: they were used minutes ago)
t0 = time.time()
d = subprocess.Popen([PY, os.path.join(ROOT, "proto", "midyd.py"), "--socket", sock_path, "--db", db], stdout=subprocess.PIPE); d.stdout.readline()
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); r, w = c.makefile("r"), c.makefile("w")
def req(o):
    w.write(json.dumps(o) + "\n"); w.flush(); return json.loads(r.readline())
warm_at, ph = {}, Phase(d.pid)
while len(warm_at) < 2:
    p = req({"cmd": "status"})["status"]["pool"]
    for k in ("asr", "llm"):
        if p[k] == "warm" and k not in warm_at:
            warm_at[k] = round(time.time() - t0, 2)
    ph.tick(); time.sleep(0.3)
ph.tick(); out["1_load"] = {"warm_after_s": warm_at, **ph.result(gpu_base)}

# 2. idle: Middy open, no meeting, both models loaded and waiting
ph, t_idle = Phase(d.pid), time.time()
while time.time() - t_idle < IDLE_S:
    time.sleep(10); ph.tick()
out["2_idle"] = ph.result(gpu_base)

# 3. meeting: 120 s of public B1 fed in real time (ASR + live notes on Gemma)
wav = os.path.join(run, "l19_in.wav")
subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "0", "-t", "120", "-i", os.environ.get("MIDY_TEST_AUDIO", "test_audio.mp3"), "-ac", "1", "-ar", "16000", wav], check=True)
x, _ = sf.read(wav, dtype="int16")
t_rec = time.time(); req({"cmd": "start", "name": "meeting 1 l19", "live": "ui", "language": "Vietnamese", "chunk_min": 10})
while not (req({"cmd": "status"})["status"].get("state") == "recording"):
    time.sleep(0.05)
out["3_record_to_ready_s"] = round(time.time() - t_rec, 2)
ph = Phase(d.pid); stop_meet = threading.Event()
def meet_sampler():
    while not stop_meet.is_set():
        ph.tick(); time.sleep(1)
threading.Thread(target=meet_sampler, daemon=True).start()
tm = time.time()
for i in range(0, len(x), 4000):
    for stream in (0, 1):
        w.write(json.dumps({"cmd": "audio", "stream": stream, "pcm": base64.b64encode(x[i:i + 4000].tobytes()).decode()}) + "\n")
    w.flush(); time.sleep(max(0.0, tm + (i + 4000) / 16000 - time.time()))
stop_meet.set(); time.sleep(1.5); ph.tick()
out["3_meeting"] = ph.result(gpu_base)

# 4. after Stop: last notes + MoM + diarization in the background, and the spare pair loading (Lỗi 15 refill at +30 s)
req({"cmd": "stop"}); ts = time.time(); ph = Phase(d.pid)
while time.time() - ts < 90:
    ph.tick(); time.sleep(1)
out["4_after_stop_90s"] = {**ph.result(gpu_base), "pool_then": req({"cmd": "status"})["status"]["pool"]}

# 5. reload with models in page cache: a second daemon from scratch (what "release after N min, reload on Record" would cost)
req({"cmd": "quit"}); time.sleep(4)
t1 = time.time()
d2 = subprocess.Popen([PY, os.path.join(ROOT, "proto", "midyd.py"), "--socket", sock_path, "--db", db], stdout=subprocess.PIPE); d2.stdout.readline()
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); r, w = c.makefile("r"), c.makefile("w")
warm2, ph = {}, Phase(d2.pid)
while len(warm2) < 2:
    p = req({"cmd": "status"})["status"]["pool"]
    for k in ("asr", "llm"):
        if p[k] == "warm" and k not in warm2:
            warm2[k] = round(time.time() - t1, 2)
    ph.tick(); time.sleep(0.3)
ph.tick(); out["5_reload_hot_cache"] = {"warm_after_s": warm2, **ph.result(gpu_base)}
req({"cmd": "quit"}); time.sleep(3)
print(json.dumps(out, indent=1))
