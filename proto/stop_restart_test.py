"""Lỗi 14 acceptance, blind (public B1 file, no app window, no speaker): meeting A is fed in real time and stopped; a new meeting B
is requested right away and retried until the daemon accepts it; B is fed while A finishes in the background. Numbers only.
  stop_restart_test.py [--a 180] [--b 120] [--name l14]
Reports: stop -> start accepted (s), start -> ready (s), both meetings' status / MoM / live-note coverage, B's ASR lag, and the peak
RAM of the daemon's process tree sampled every 0.5 s (plus how many Gemma / Qwen workers ran at the same time).
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time

import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
B1 = os.environ.get("MIDY_TEST_AUDIO", "test_audio.mp3")
ap = argparse.ArgumentParser()
ap.add_argument("--a", type=float, default=180); ap.add_argument("--b", type=float, default=120); ap.add_argument("--name", default="l14")
A = ap.parse_args()
run = os.path.join(HERE, "..", "run"); sock_path = os.path.join(run, f"{A.name}.sock"); db = os.path.join(run, f"{A.name}.db")
for f in (db, db + "-wal", db + "-shm"):
    if os.path.exists(f):
        os.remove(f)


def decode(ss, dur):
    out = os.path.join(run, f"{A.name}_{int(ss)}.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(ss), "-t", str(dur), "-i", B1, "-ac", "1", "-ar", "16000", out], check=True)
    x, _ = sf.read(out, dtype="int16"); return x


feeds = {"A": (decode(60, A.a), decode(360, A.a)), "B": (decode(600, A.b), decode(900, A.b))}   # (system, mic), all public audio
d = subprocess.Popen([sys.executable, os.path.join(HERE, "midyd.py"), "--socket", sock_path, "--db", db], stdout=subprocess.PIPE)
print(d.stdout.readline().decode().strip(), flush=True)


def conn():
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); return c, c.makefile("r"), c.makefile("w")


c, r, w = conn()
def req(o):
    w.write(json.dumps(o) + "\n"); w.flush(); return json.loads(r.readline())


# ---- RAM sampler: the daemon's whole process tree (ps runs here, outside the daemon's sandbox) -------------------------
peak = {"rss_gb": 0.0, "llm_workers": 0, "asr_workers": 0, "memlevel_min": 100}
stop_sampling = threading.Event()
def tree():
    rows = [l.split(None, 3) for l in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,command="], capture_output=True, text=True).stdout.splitlines()]
    kids, t = {}, set()
    for p in rows:
        kids.setdefault(p[1], []).append(p)
    todo = [str(d.pid)]
    while todo:
        pid = todo.pop(); t.add(pid); todo += [k[0] for k in kids.get(pid, [])]
    return [p for p in rows if p[0] in t]


def sampler():
    while not stop_sampling.is_set():
        mine = tree()
        peak["rss_gb"] = max(peak["rss_gb"], round(sum(int(p[2]) for p in mine) / 1e6, 2))
        peak["llm_workers"] = max(peak["llm_workers"], sum("llm_worker.py" in p[3] for p in mine))
        peak["asr_workers"] = max(peak["asr_workers"], sum("asr_worker.py" in p[3] for p in mine))
        lvl = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout.strip()
        peak["memlevel_min"] = min(peak["memlevel_min"], int(lvl or 100))
        stop_sampling.wait(0.5)
threading.Thread(target=sampler, daemon=True).start()

def pinger():                                            # how long the daemon ever takes to answer: a frozen daemon = a frozen meeting
    cp, rp, wp = conn()
    while not stop_sampling.is_set():
        t = time.time(); wp.write(json.dumps({"cmd": "status"}) + "\n"); wp.flush(); rp.readline()
        peak["status_rtt_max_s"] = max(peak.get("status_rtt_max_s", 0.0), round(time.time() - t, 2))
        stop_sampling.wait(0.25)
threading.Thread(target=pinger, daemon=True).start()

done, first_live, ready_at = {}, {}, {}
def subscribe(tag):
    c2, r2, w2 = conn()
    w2.write(json.dumps({"cmd": "subscribe"}) + "\n"); w2.flush(); r2.readline()
    for line in r2:
        ev = json.loads(line)
        if ev.get("type") == "note" and ev.get("kind") == "live" and tag not in first_live:
            first_live[tag] = time.time()
        if ev.get("type") == "done":
            done[tag] = (time.time(), ev); break


def meeting(tag):
    """start (retried until accepted) -> ready -> real-time feed. Returns timings."""
    t_ask = time.time(); tries = 0
    while True:
        tries += 1
        rep = req({"cmd": "start", "name": f"meeting 2026 {A.name}_{tag}", "live": "ui", "language": "Vietnamese", "chunk_min": 10})
        if rep.get("ok"):
            break
        if time.time() - t_ask > 600:
            raise SystemExit(f"start {tag} refused for 600 s: {rep}")
        time.sleep(0.2)
    t_ok = time.time()
    threading.Thread(target=subscribe, args=(tag,), daemon=True).start()
    while True:                                          # the app keeps chunks in its pre-buffer until "ready"; so do we
        st = req({"cmd": "status"})["status"]
        if st.get("state") == "recording" and st.get("meeting_id") and st["meeting_id"] not in ids.values():
            break
        time.sleep(0.05)
    ids[tag] = st["meeting_id"]; t_ready = ready_at[tag] = time.time()
    sysx, micx = feeds[tag]; n = 4000
    for i in range(0, len(sysx), n):
        for stream, x in ((0, sysx), (1, micx)):
            w.write(json.dumps({"cmd": "audio", "stream": stream, "pcm": base64.b64encode(x[i:i + n].tobytes()).decode()}) + "\n")
        w.flush()
        time.sleep(max(0.0, t_ready + (i + n) / 16000 - time.time()))
    t_stop = time.time(); req({"cmd": "stop"})
    return {"start_tries": tries, "start_wait_s": round(t_ok - t_ask, 2), "start_to_ready_s": round(t_ready - t_ok, 2), "t_stop": t_stop}


ids = {}
t_launch = time.time(); warm = {}
while "pool" in req({"cmd": "status"})["status"]:        # Lỗi 15: Middy prewarms at launch; measure it, then the idle footprint
    ps = req({"cmd": "status"})["status"]["pool"]
    if ps == {"asr": "warm", "llm": "warm"}:
        mine = tree(); lvl = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout.strip()
        warm = {"prewarm_s": round(time.time() - t_launch, 1), "idle_rss_gb": round(sum(int(p[2]) for p in mine) / 1e6, 2), "idle_memlevel": int(lvl),
                "idle_workers": sum("worker.py" in p[3] for p in mine)}
        break
    time.sleep(0.5)
ta = meeting("A")
tb = meeting("B")                                        # asked right after A's stop: the number the user waits for
t_end = time.time()
while len(done) < 2 and time.time() - t_end < 900:
    time.sleep(0.5)
stop_sampling.set()
out = {"warm_at_launch": warm, "A_start_to_ready_s": ta["start_to_ready_s"], "pool_at_end": req({"cmd": "status"})["status"].get("pool"),
       "stop_A_to_start_B_accepted_s": round(tb["start_wait_s"], 2), "start_B_tries": tb["start_tries"], "B_start_to_ready_s": tb["start_to_ready_s"],
       "A_done_after_stop_s": round(done["A"][0] - ta["t_stop"], 1) if "A" in done else None,
       "A_done_before_B_start": bool("A" in done and done["A"][0] < ta["t_stop"] + tb["start_wait_s"] + 0.01), "peak": peak}
for tag in ("A", "B"):
    m = req({"cmd": "meeting", "meeting_id": ids[tag]})
    stp = os.path.join(run, f"meeting 2026 {A.name}_{tag}", "stats.json")
    for _ in range(120):                                 # "done" is published just before stats.json is written
        if os.path.exists(stp):
            break
        time.sleep(0.5)
    time.sleep(0.5); st = json.load(open(stp)) if os.path.exists(stp) else {}
    ls, asr = st.get("live_summary", {}), st.get("asr", {})
    out[tag] = {"warm_workers": st.get("warm_workers"), "status": m["meeting"]["status"], "name_from_mom": m["meeting"]["name"] != f"meeting 2026 {A.name}_{tag}", "segments": len(m["segments"]),
                "notes": sorted({n["kind"] for n in m["notes"]}), "mom_chars": len(next((n["text"] for n in m["notes"] if n["kind"] == "mom"), "")),
                "blocks_refined": ls.get("blocks_refined_with_text"), "blocks_not_summarised": ls.get("blocks_refined_not_summarised"),
                "live_notes": ls.get("n"), "asr_queue_lag_s": asr.get("queue_lag_s"), "asr_lag_peaks_over_1s": asr.get("lag_peaks_over_1.0s"),
                "llm_pause_events": st.get("llm", {}).get("pause_events"), "first_live_note_after_ready_s": round(first_live[tag] - ready_at[tag], 1) if tag in first_live else None, "capture_rate_min": {k: v.get("min") for k, v in st.get("capture", {}).get("rate", {}).items()} if isinstance(st.get("capture"), dict) else None}
req({"cmd": "quit"})
print(json.dumps(out, ensure_ascii=False))
