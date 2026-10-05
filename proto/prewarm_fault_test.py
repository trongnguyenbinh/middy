"""Lỗi 15 fault test, blind: the warm workers the daemon keeps for the next meeting are (1) killed, (2) frozen (SIGSTOP, what a
worker stuck after sleep would look like). A meeting started then must fall back to fresh workers and finish, never hang.
  prewarm_fault_test.py            numbers only
"""
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time

import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
run = os.path.join(HERE, "..", "run"); sock_path = os.path.join(run, "l15f.sock"); db = os.path.join(run, "l15f.db")
for f in (db, db + "-wal", db + "-shm"):
    if os.path.exists(f):
        os.remove(f)
wav = os.path.join(run, "l15f_in.wav")
subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "60", "-t", "20", "-i", os.environ.get("MIDY_TEST_AUDIO", "test_audio.mp3"),
                "-ac", "1", "-ar", "16000", wav], check=True)
x, _ = sf.read(wav, dtype="int16")
d = subprocess.Popen([sys.executable, os.path.join(HERE, "midyd.py"), "--socket", sock_path, "--db", db], stdout=subprocess.PIPE)
d.stdout.readline()
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); r, w = c.makefile("r"), c.makefile("w")


def req(o):
    w.write(json.dumps(o) + "\n"); w.flush(); return json.loads(r.readline())


def warm_pids():
    """Warm workers of THIS test's daemon only (never the user's Middy, never a parallel test)."""
    rows = [l.split(None, 2) for l in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout.splitlines()]
    return [int(p[0]) for p in rows if p[1] == str(d.pid) and ("asr_worker.py --warm" in p[2] or "warm_llm_" in p[2])]


def fail(msg):                                   # never leave the test daemon (and its warm workers) behind
    d.kill(); raise SystemExit(msg)


def wait_warm(limit=180):
    t = time.time()
    while req({"cmd": "status"})["status"]["pool"] != {"asr": "warm", "llm": "warm"}:
        if time.time() - t > limit:
            fail("pool never warm")
        time.sleep(0.5)


out = {}
for case, sig in (("killed", signal.SIGKILL), ("frozen", signal.SIGSTOP)):
    wait_warm()
    pids = warm_pids()
    for p in pids:
        os.kill(p, sig)
    t = time.time(); req({"cmd": "start", "name": f"meeting 1 l15f {case}", "live": "ui", "language": "Vietnamese", "chunk_min": 10})
    while True:
        st = req({"cmd": "status"})["status"]
        if st.get("state") == "recording" and st.get("meeting_id") and st["meeting_id"] not in [o["meeting_id"] for o in out.values()]:
            break
        if time.time() - t > 240:
            fail(f"{case}: never ready")
        time.sleep(0.1)
    ready_s = round(time.time() - t, 1)
    for i in range(0, len(x), 4000):
        for stream in (0, 1):
            w.write(json.dumps({"cmd": "audio", "stream": stream, "pcm": base64.b64encode(x[i:i + 4000].tobytes()).decode()}) + "\n")
        w.flush(); time.sleep(0.25)
    req({"cmd": "stop"})
    mid = st["meeting_id"]
    while req({"cmd": "meeting", "meeting_id": mid})["meeting"]["status"] in ("recording", "finishing"):
        time.sleep(1)
    m = req({"cmd": "meeting", "meeting_id": mid})
    left = [p for p in pids if subprocess.run(["ps", "-p", str(p)], capture_output=True).returncode == 0]
    out[case] = {"meeting_id": mid, "start_to_ready_s": ready_s, "status": m["meeting"]["status"], "segments": len(m["segments"]),
                 "notes": sorted({n["kind"] for n in m["notes"]}), "faulty_workers_left_running": len(left), "faulty_workers": len(pids)}
    for p in left:
        os.kill(p, signal.SIGKILL)
req({"cmd": "quit"})
time.sleep(3)
out["workers_left_after_quit"] = len(warm_pids())
print(json.dumps(out))
