"""Exercise the daemon's UI-fed path without the Electron app: start midyd on a private socket, push the public B1 file as
mic (stream 1) and a shifted copy as system (stream 0) in real time (250 ms s16le chunks, base64 JSON like the UI), subscribe
from a second connection and count events, then stop and wait for `done`. Numbers only.
  ui_feed_test.py [--start 60 --end 150 --mic-offset 300] [--mic-zero-after S]
--mic-zero-after S: from S seconds on, the mic stream carries digital silence, exactly what Middy main sends when the mic-input
button is off (Lỗi 9); the report then counts mic segments that START after S.
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

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
B1 = os.environ.get("MIDY_TEST_AUDIO", "test_audio.mp3")
ap = argparse.ArgumentParser()
ap.add_argument("--start", type=float, default=60); ap.add_argument("--end", type=float, default=150)
ap.add_argument("--mic-offset", type=float, default=300); ap.add_argument("--name", default="m2_uifeed")
ap.add_argument("--mic-zero-after", type=float, default=None)
ap.add_argument("--switch-language-at", type=float, default=None); ap.add_argument("--switch-to", default="English")   # Lỗi 10
A = ap.parse_args()
run = os.path.join(HERE, "..", "run"); sock_path = os.path.join(run, "midy_test.sock")
wav = os.path.join(run, f"{A.name}_in.wav")


def decode(ss, dur):
    out = os.path.join(run, f"{A.name}_{int(ss)}.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(ss), "-t", str(dur), "-i", B1, "-ac", "1", "-ar", "16000", out], check=True)
    x, _ = sf.read(out, dtype="int16"); return x


dur = A.end - A.start
sysx, micx = decode(A.start, dur), decode(A.start + A.mic_offset, dur)
d = subprocess.Popen([sys.executable, os.path.join(HERE, "midyd.py"), "--socket", sock_path, "--db", os.path.join(run, f"{A.name}.db")], stdout=subprocess.PIPE)
print(d.stdout.readline().decode().strip())


def conn():
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); return c, c.makefile("r"), c.makefile("w")


c, r, w = conn()
w.write(json.dumps({"cmd": "start", "name": A.name, "live": "ui", "language": "Vietnamese", "chunk_min": 0.75}) + "\n"); w.flush()
print(r.readline().strip())
counts, first_partial_wall, t_go = {}, [None], [None]
warns = []


def sub():
    c2, r2, w2 = conn()
    w2.write(json.dumps({"cmd": "subscribe"}) + "\n"); w2.flush(); r2.readline()
    for line in r2:
        ev = json.loads(line); t = ev.get("type")
        counts[t] = counts.get(t, 0) + 1
        if t == "warn":
            warns.append({k: ev.get(k) for k in ("where", "stream", "lag_s", "capture_rate", "error")})
        if t == "transcript" and ev.get("text") and first_partial_wall[0] is None:
            first_partial_wall[0] = time.time()
        if t == "done":
            break


threading.Thread(target=sub, daemon=True).start()
# wait for ready
while True:
    w.write(json.dumps({"cmd": "status"}) + "\n"); w.flush(); st = json.loads(r.readline())["status"]
    if st.get("state") == "recording" and st.get("meeting_id"):
        break
    time.sleep(0.5)
t_go[0] = time.time(); n = 4000; switched = [None]
for i in range(0, len(sysx), n):
    if A.switch_language_at is not None and switched[0] is None and i / 16000 >= A.switch_language_at:
        w.write(json.dumps({"cmd": "set_language", "language": A.switch_to}) + "\n"); w.flush(); switched[0] = json.loads(r.readline())
    for stream, x in ((0, sysx), (1, micx)):
        chunk = x[i:i + n]
        if stream == 1 and A.mic_zero_after is not None and i / 16000 >= A.mic_zero_after:
            chunk = np.zeros_like(chunk)
        w.write(json.dumps({"cmd": "audio", "stream": stream, "pcm": base64.b64encode(chunk.tobytes()).decode()}) + "\n")
    w.flush()
    time.sleep(max(0.0, t_go[0] + (i + n) / 16000 - time.time()))
t_sent = time.time()
w.write(json.dumps({"cmd": "stop"}) + "\n"); w.flush(); print(r.readline().strip())
while "done" not in counts and time.time() - t_sent < 300:
    time.sleep(0.5)
w.write(json.dumps({"cmd": "status"}) + "\n"); w.flush(); st = json.loads(r.readline())["status"]
w.write(json.dumps({"cmd": "meeting", "meeting_id": st["meeting_id"]}) + "\n"); w.flush(); m = json.loads(r.readline())
w.write(json.dumps({"cmd": "quit"}) + "\n"); w.flush()
print(json.dumps({"events": counts, "audio_s": st.get("audio_s"), "finals": st.get("finals"), "first_partial_after_go_s": round(first_partial_wall[0] - t_go[0], 2) if first_partial_wall[0] else None,
                  "segments_in_db": len(m["segments"]), "mic_segments": sum(1 for x in m["segments"] if x.get("stream") == "mic"),
                  "mic_segments_starting_after_zero": (sum(1 for x in m["segments"] if x.get("stream") == "mic" and x.get("s", 0) >= A.mic_zero_after) if A.mic_zero_after is not None else None),
                  "system_segments": sum(1 for x in m["segments"] if x.get("stream") != "mic"), "notes_in_db": [(x["kind"], x["idx"]) for x in m["notes"]], "tail_wall_s": round(time.time() - t_sent, 1), "warns": warns}))
if A.switch_language_at is not None:                     # Lỗi 10: which language recognised each final, relative to the switch
    T = A.switch_language_at
    fin = [json.loads(l) for l in open(os.path.join(run, A.name, "events.jsonl")) if '"type": "final"' in l]
    before = [f for f in fin if f["e"] <= T]; after = [f for f in fin if f["s"] >= T]; straddle = [f for f in fin if f["s"] < T < f["e"]]
    applied = [json.loads(l) for l in open(os.path.join(run, A.name, "events.jsonl")) if '"language_applied"' in l]
    print(json.dumps({"switch_reply": switched[0], "language_applied_events": len(applied), "finals": len(fin),
                      "before_switch": {"n": len(before), "langs": sorted({f["asr_language"] for f in before})},
                      "after_switch": {"n": len(after), "langs": sorted({f["asr_language"] for f in after})},
                      "straddling": [{"s": f["s"], "e": f["e"], "asr_language": f["asr_language"]} for f in straddle]}))
d.wait(timeout=30)
