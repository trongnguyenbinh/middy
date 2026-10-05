"""Việc 18 acceptance, blind: bulk delete on a TEST database (run/l18.db, never run/midy.db) with synthetic meetings only.
Cases: 1 / many (+ an unknown id) / one still recording / one still finishing (just stopped) / all; plus a rollback check.
Checks after each: rows (meetings, notes, segments, slides), FTS search, and the meeting directories on disk.
  bulk_delete_test.py            numbers only
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from store import Store  # noqa: E402

run = os.path.realpath(os.path.join(HERE, "..", "run"))
db = os.path.join(run, "l18.db")
for f in (db, db + "-wal", db + "-shm"):
    if os.path.exists(f):
        os.remove(f)

# ---- rollback: a failure in the middle of a batch leaves every meeting in place --------------------------------------------
tmp = tempfile.mkdtemp(); st = Store(os.path.join(tmp, "t.db"))
a, b = st.new_meeting("a", "default", "English", {}), st.new_meeting("b", "default", "English", {})
real = st.delete_meeting
st.delete_meeting = lambda mid: (_ for _ in ()).throw(RuntimeError("boom")) if mid == b else real(mid)
try:
    st.delete_meetings([a, b]); rolled_back = False
except RuntimeError:
    rolled_back = st.meeting(a) is not None and st.meeting(b) is not None
shutil.rmtree(tmp)

# ---- synthetic meetings: segments (FTS), notes, a run/ directory each; one legacy meeting without run_dir -------------------
st = Store(db)
ids, dirs, words = [], {}, {}
for i in range(6):
    mid = st.new_meeting(f"l18 meeting {i}", "default", "English", {"live": "ui"})
    d = os.path.join(run, f"l18_meeting_{i}"); os.makedirs(d, exist_ok=True); open(os.path.join(d, "transcript.md"), "w").write("x")
    st.set_run_dir(mid, d)
    w = f"zebraword{i}"
    st.add_segment(mid, {"id": 1, "s": 0.0, "e": 2.0, "speaker": "Speaker 1", "text": f"hello {w} world", "dropped_lang": False})
    st.set_note(mid, "mom", 0, f"# MoM {i}"); st.set_note(mid, "live", 0, "notes")
    ids.append(mid); dirs[mid] = d; words[mid] = w
legacy = st.new_meeting("l18 legacy", "default", "English", {"live": "ui"})       # recorded before run_dir existed
st.db.execute("update meetings set started_at=? where id=?", (978307200.0, legacy))  # 2001-01-01 00:00 UTC: no clash with real dirs
ldir = os.path.join(run, "meeting 2001-01-01T00:00"); os.makedirs(ldir, exist_ok=True)
st.set_note(legacy, "mom", 0, "# legacy")
st.db.close()

sock = os.path.join(run, "l18.sock")
d = subprocess.Popen([sys.executable, os.path.join(HERE, "midyd.py"), "--socket", sock, "--db", db], stdout=subprocess.PIPE); d.stdout.readline()
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock); r, w_ = c.makefile("r"), c.makefile("w")


def req(o):
    w_.write(json.dumps(o) + "\n"); w_.flush(); return json.loads(r.readline())


def clean(mids):
    """Every trace of these meetings is gone: rows, FTS hits, directories."""
    chk = Store(db)
    rows = {t: chk.db.execute(f"select count(*) from {t} where meeting_id in ({','.join('?' * len(mids))})", mids).fetchone()[0] for t in ("notes", "segments", "slides")}
    rows["meetings"] = chk.db.execute(f"select count(*) from meetings where id in ({','.join('?' * len(mids))})", mids).fetchone()[0]
    fts = sum(len(req({"cmd": "search", "q": words[m]})["hits"]) for m in mids if m in words)
    left_dirs = sum(os.path.isdir(dirs[m]) for m in mids if m in dirs) + (os.path.isdir(ldir) if legacy in mids else 0)
    return rows | {"fts_hits": fts, "dirs_left": left_dirs}


out = {"rollback_keeps_all": rolled_back}
out["1_one"] = {"reply": req({"cmd": "delete_meetings", "meeting_ids": [ids[0]]}), "clean": clean([ids[0]])}
out["2_many_plus_unknown"] = {"reply": req({"cmd": "delete_meetings", "meeting_ids": [ids[1], ids[2], 999]}), "clean": clean([ids[1], ids[2]])}
while req({"cmd": "status"})["status"]["pool"] != {"asr": "warm", "llm": "warm"}:
    time.sleep(0.5)
req({"cmd": "start", "name": "meeting 1 l18 running", "live": "ui", "language": "English", "chunk_min": 10})
while True:
    s = req({"cmd": "status"})["status"]
    if s.get("state") == "recording" and s.get("meeting_id"):
        break
    time.sleep(0.1)
rid = s["meeting_id"]
rep = req({"cmd": "delete_meetings", "meeting_ids": [rid, ids[3]]})
out["3_one_recording"] = {"reply": rep, "recording_still_there": req({"cmd": "meeting", "meeting_id": rid})["meeting"] is not None, "clean": clean([ids[3]])}
req({"cmd": "stop"})
rep = req({"cmd": "delete_meetings", "meeting_ids": [rid]})
out["4_just_stopped"] = {"reply": rep, "status_then": req({"cmd": "meeting", "meeting_id": rid})["meeting"]["status"]}
while req({"cmd": "meeting", "meeting_id": rid})["meeting"]["status"] in ("recording", "finishing"):
    time.sleep(0.5)
rest = [m["id"] for m in req({"cmd": "meetings"})["meetings"]]
dirs[rid] = os.path.join(run, "meeting 1 l18 running")
out["5_all"] = {"asked": len(rest), "reply": req({"cmd": "delete_meetings", "meeting_ids": rest}), "clean": clean(rest),
                "meetings_left": len(req({"cmd": "meetings"})["meetings"])}
chk = Store(db)
out["tables_after"] = {t: chk.db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("meetings", "notes", "segments", "slides", "segments_fts")}
req({"cmd": "quit"})
print(json.dumps(out, indent=1))
