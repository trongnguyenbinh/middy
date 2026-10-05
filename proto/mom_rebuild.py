"""Rebuild a finished meeting's MoM on this machine (local Gemma, Unix socket, no network) — Lỗi 11, option C since Lỗi 21c:
the MoM reads the meeting's transcript in 600 s parts (mom_c.py), the same code the daemon runs after Stop.
  step 1:  mom_rebuild.py <run-dir> [--language Vietnamese]          -> writes <run-dir>/mom_<language>.md only, prints numbers
  step 2:  mom_rebuild.py <run-dir> [--language …] --meeting-id N --apply
           puts THAT reviewed file into the library (SQLite notes, kind "mom") without running Gemma again, and renames the
           meeting when its name was taken from the old MoM title; the old MoM stays in <run-dir>/mom.md.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from glossary import VI  # noqa: E402
from store import Store  # noqa: E402
import mom_c  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_dir")
ap.add_argument("--language", default="Vietnamese")
ap.add_argument("--meeting-id", type=int)
ap.add_argument("--apply", action="store_true", help="step 2: write the reviewed mom_<language>.md into the library")
ap.add_argument("--db", default=os.path.join(HERE, "..", "run", "midy.db"))
A = ap.parse_args()
if A.apply and not A.meeting_id:
    sys.exit("--apply needs --meeting-id")
if os.environ.get("MIDY_SANDBOX") != "1":             # same no-network sandbox as the daemon (run_m0.py): meeting text never leaves
    os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", os.path.join(HERE, "nonet.sb"), "-D", "RUN=" + os.path.realpath(os.path.join(HERE, "..", "run")), sys.executable] + sys.argv, dict(os.environ, MIDY_SANDBOX="1"))
out = os.path.join(A.run_dir, f"mom_{A.language}.md")

if A.apply:
    if not os.path.exists(out):
        sys.exit(f"run step 1 first: {out} does not exist")
    text = open(out).read()
    st = Store(A.db)
    st.set_note(A.meeting_id, "mom", 0, text, {"rebuilt": "mom_c", "file": out})
    title = re.sub(r"^#\s*", "", text.split("\n")[0]).strip()[:120] if text.startswith("# ") else None   # same rule as core._finish
    old_p = os.path.join(A.run_dir, "mom.md")
    old = re.sub(r"^#\s*", "", open(old_p).read().split("\n")[0]).strip()[:120] if os.path.exists(old_p) else None
    m = st.meeting(A.meeting_id)
    renamed = bool(title and m and old and m["name"] == old)  # the name came from the old MoM title, not typed by the user
    if renamed:
        st.set_name(A.meeting_id, title)
    print(json.dumps({"applied": {"meeting_id": A.meeting_id, "renamed": renamed, "file": out}}))
    sys.exit()

finals = [json.loads(l) for l in open(os.path.join(A.run_dir, "events.jsonl")) if '"type": "final"' in l]
if not [f for f in finals if f["text"] and not f.get("dropped_lang")]:
    sys.exit("no recognised text in this meeting")
sock = os.path.join(A.run_dir, "rebuild.sock")
if os.path.exists(sock):
    os.unlink(sock)
w = subprocess.Popen([sys.executable, os.path.join(HERE, "llm_worker.py"), "--socket", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(600):
    if os.path.exists(sock):
        break
    time.sleep(0.1)
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock); r, f = c.makefile("r"), c.makefile("w")
json.loads(r.readline())


def generate(tag, prompt, max_tokens):
    f.write(json.dumps({"cmd": "generate", "id": tag, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}) + "\n"); f.flush()
    while True:
        e = json.loads(r.readline())
        if e.get("event") == "done":
            return e["text"], e["stats"]


t0 = time.time()
mom, parts, info = mom_c.build_mom(finals, A.language, generate)
f.write(json.dumps({"cmd": "quit"}) + "\n"); f.flush(); w.wait(timeout=30)
if os.path.exists(sock):
    os.unlink(sock)
open(out, "w").write(mom)
letters = sum(ch.isalpha() for ch in mom)
print(json.dumps({"finals": len(finals), **info, "wall_s": round(time.time() - t0, 1), "mom_lines": mom.count("\n") + 1,
                  "mom_vi_ratio": round(len(VI.findall(mom)) / max(letters, 1), 3), "sections": len(re.findall(r"^## ", mom, re.M)), "file": out}, ensure_ascii=False))
