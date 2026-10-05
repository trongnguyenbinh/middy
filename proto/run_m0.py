"""Measurement CLI: run one meeting (a recording played in real time, or live capture) through the core Session and
print stats.json (numbers only; transcript, notes and MoM stay in run/<name>/ and in the SQLite store).

By default the whole process tree is re-executed under the sandbox profile proto/nonet.sb (no IP networking,
Unix sockets allowed) — design 1.9, enforcement rather than sampling.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--input", help="video/audio file; omit with --live")
ap.add_argument("--live", action="store_true", help="capture mic + system audio with tools/audiocap")
ap.add_argument("--name", required=True)
ap.add_argument("--start", type=float, default=0.0)
ap.add_argument("--end", type=float, default=None)
ap.add_argument("--language", default="English")
ap.add_argument("--speed", type=float, default=1.0, help="debug only; latencies are meaningful at 1.0")
ap.add_argument("--llm-policy", choices=["free", "guard", "silence"], default="guard")
ap.add_argument("--no-llm", action="store_true")
ap.add_argument("--no-slides", action="store_true")
ap.add_argument("--chunk-min", type=float, default=10.0)
ap.add_argument("--diar-threshold", type=float, default=0.8)
ap.add_argument("--num-speakers", type=int, default=-1, help="participants count if known; -1 = cluster by threshold")
ap.add_argument("--vad-max-speech", type=float, default=25.0)
ap.add_argument("--space", default="default")
ap.add_argument("--db", default=None, help="SQLite path (default run/midy.db)")
ap.add_argument("--resume", type=int, default=None, help="meeting id to continue after a crash")
ap.add_argument("--duration", type=float, default=None, help="live: stop automatically after N seconds")
ap.add_argument("--capture", choices=["audiotee", "audiocap", "ffmpeg"], default="audiotee", help="audiotee: system=audiotee + mic=AVAudioEngine; ffmpeg: old mic path (comparison); audiocap: both via tools/audiocap")
ap.add_argument("--mic-offset", type=float, default=0.0, help="file mode: feed the mic stream with the same recording shifted by N s (two-stream regression)")
ap.add_argument("--no-sandbox", action="store_true", help="do NOT enforce the no-network sandbox (measurement only)")
A = ap.parse_args()

if not A.no_sandbox and os.environ.get("MIDY_SANDBOX") != "1":
    os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", os.path.join(HERE, "nonet.sb"), "-D", "RUN=" + os.path.realpath(os.path.join(HERE, "..", "run")), sys.executable] + sys.argv, dict(os.environ, MIDY_SANDBOX="1"))

from core import Session  # noqa: E402

s = Session(name=A.name, input=A.input, live=A.live, start=A.start, end=A.end, language=A.language, speed=A.speed, llm_policy=A.llm_policy,
            no_llm=A.no_llm, no_slides=A.no_slides, chunk_min=A.chunk_min, diar_threshold=A.diar_threshold,
            num_speakers=A.num_speakers, vad_max_speech=A.vad_max_speech, space=A.space, db=A.db, resume=A.resume, duration=A.duration, capture=A.capture, mic_offset=A.mic_offset)
print(json.dumps(s.run(), indent=1))
