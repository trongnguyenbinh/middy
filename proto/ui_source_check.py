"""Self-check of UiSource.check_drift (rnd M2 v1 item 8): a stream that stops delivering gets silence inserted so both streams
keep one time axis, one warning at start and one at end. Numbers only.  python proto/ui_source_check.py"""
import io
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
from sources import UiSource  # noqa: E402

buf, stop, warns = io.BytesIO(), threading.Event(), []
u = UiSource(buf, stop, on_warn=warns.append)
pcm = np.zeros(4000, np.int16).tobytes()
u.feed(0, pcm); u.feed(1, pcm)                       # both streams at 0.25 s, t0_wall set
u.t0_wall -= 5.0                                     # pretend 5 s of wall time passed
for _ in range(20):
    u.feed(0, pcm)                                   # system keeps delivering (5.25 s); mic stuck at 0.25 s
u.check_drift()
assert u.silence_inserted[1] > 3.5 and u.silence_inserted[0] == 0 and abs(u.t_end[1] - (5.25 - 0.5)) < 0.3
assert len(warns) == 1 and warns[0]["where"] == "stream_drift" and warns[0]["stream"] == "mic"
u.check_drift()                                      # lag now 0.75 s: below the 1 s "back to normal" mark -> end warning once
assert len(warns) == 2 and warns[1]["where"] == "stream_drift_end"
u.check_drift(); assert len(warns) == 2
# rnd M2 v2 item 5: a 40 s pre-buffer flushed in a burst, with the 1 s drift tick firing at 1/3 of the flush -> NO false silence
import time
buf2, stop2, warns2 = io.BytesIO(), threading.Event(), []
v = UiSource(buf2, stop2, on_warn=warns2.append)
t_first = time.time() - 40.0                                 # captured 40 s ago, arriving now
n = 160                                                      # 160 chunks x 0.25 s = 40 s per stream
for i in range(n):
    tc = t_first + (i + 1) * 0.25
    v.feed(0, pcm, tc); v.feed(1, pcm, tc)
    if i == n // 3:
        v.check_drift()                                      # tick lands mid-flush
v.check_drift()
assert v.silence_inserted == {0: 0.0, 1: 0.0} and not warns2, (v.silence_inserted, warns2)
# ...and a stream that really stops (mic) while the other keeps arriving is still caught
for i in range(n, n + 20):
    v.feed(0, pcm, t_first + (i + 1) * 0.25)                 # system continues 5 s; mic silent
v.check_drift()
assert v.silence_inserted[1] > 3.5 and warns2 and warns2[0]["stream"] == "mic", (v.silence_inserted, warns2)
print(json.dumps({"flush_case": {"silence_inserted_s": v.silence_inserted, "warns": [w["where"] for w in warns2]}}))
print(json.dumps({"t_end": u.t_end, "silence_inserted_s": {k: round(v, 2) for k, v in u.silence_inserted.items()}, "warns": [w["where"] for w in warns]}))
print("ui_source drift self-check OK")
