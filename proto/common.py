"""Shared constants and the raw PCM frame protocol between the player and the ASR worker."""
import json
import struct
import sys
import time

SR = 16000
FRAME_S = 0.25                     # 250 ms frames, same chunking as the reference app (spec B.1/B.2)
STREAMS = {0: "system", 1: "mic"}  # the reference app: source = "mic" | "system"
HDR = struct.Struct("<BddI")       # stream id, audio time at frame end (s), wall clock when sent, n samples (float32 follow)
EOF_HDR = HDR.pack(255, 0.0, 0.0, 0)


def write_frame(fd, stream, t_end, samples):
    fd.write(HDR.pack(stream, t_end, time.time(), len(samples)))
    fd.write(samples.astype("float32").tobytes())


def read_frame(fd):
    h = fd.read(HDR.size)
    if len(h) < HDR.size:
        return None
    stream, t_end, t_sent, n = HDR.unpack(h)
    if stream == 255:
        return None
    import numpy as np
    return stream, t_end, t_sent, np.frombuffer(fd.read(4 * n), np.float32)


def emit(obj, fd=None):
    """One JSON event per line. Never print transcript text anywhere but the event stream (it goes to disk)."""
    fd = fd or sys.stdout
    fd.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fd.flush()


def now():
    return time.time()
