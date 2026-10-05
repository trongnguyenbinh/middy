"""Midy daemon: the core process the UI (M2) talks to. One Unix domain socket, newline-delimited JSON, no TCP.
Runs under the no-network sandbox like run_m0.py.

Requests (one JSON object per line) -> one JSON reply per line; `subscribe` turns the connection into an event stream.
  {"cmd":"start", "name":..., "file":path|null, "live":bool, "start":s, "end":s, "language":"English", "space":"default",
   "num_speakers":-1}                      -> {"ok":true, "meeting_id":null}   (the id arrives in the "ready" event / status)
  {"cmd":"stop"}                            -> {"ok":true}   (finishing: last notes, MoM, diarization; then event "done")
                                             Lỗi 14: finishing runs in the BACKGROUND; "start" is accepted right after "stop"
  {"cmd":"status"}                          -> {"ok":true, "status":{...}}
  {"cmd":"subscribe"}                       -> stream of events: ready, transcript(source,is_final,...), slide, chunk, note, idle, warn, done
  {"cmd":"transcript","meeting_id":N,"since":seq} -> {"ok":true,"segments":[...]}
  {"cmd":"notes","meeting_id":N}            -> {"ok":true,"notes":[...]}
  {"cmd":"meetings"}                        -> {"ok":true,"meetings":[...]}
  {"cmd":"search","q":"..."}                -> {"ok":true,"hits":[...]}     (FTS5 over final segments)
  {"cmd":"glossary_add","space":"*","kind":"correction|term|ambiguous","wrong":"...","right":"..."} -> {"ok":true}
  {"cmd":"glossary","space":"default"}      -> {"ok":true,"glossary":{...}}
  {"cmd":"audio","stream":0|1,"t":<wall s at chunk end, optional>,"pcm":"<base64 s16le 16 kHz>"}   (live:"ui"; NO reply; 0=system, 1=mic)
  {"cmd":"meeting","meeting_id":N}          -> {"ok":true,"meeting":{...}}
  {"cmd":"ask","question":"..."}          -> {"ok":true,"id":"askN"}  (Việc 17; events ask_delta {id,text} ... ask_done {id,text})
  {"cmd":"set_name","meeting_id":N,"name":"..."} / {"cmd":"set_space",...,"space":"..."} -> {"ok":true}
  {"cmd":"save_note","meeting_id":N,"text":"..."}  -> {"ok":true}   (user-edited note, kind "user")
  {"cmd":"spaces"}                          -> {"ok":true,"spaces":[...]}
  {"cmd":"export_docx","meeting_id":N,"path":"...docx"} -> {"ok":true,...numbers}  (Việc 13: company Word MoM, local Gemma, 20-40 s;
                                             refused while a meeting runs; use its own connection)
  {"cmd":"delete_meetings","meeting_ids":[N,...]} -> {"ok":true,"deleted":[...],"skipped_running":[...],"unknown":[...],"removed_dirs":n,"left":{...}}
                                             (Việc 18: one transaction; meetings still recording / finishing are skipped, not the batch)
  {"cmd":"save_minutes","meeting_id":N,"text":"# ...","form":{...}|null} -> {"ok":true}  (minutes from Claude Code via the MCP server;
                                             form = the Word fields, then export_docx needs no Gemma; also "summarizer":"local|claude" on start)
  {"cmd":"delete_meeting","meeting_id":N}   -> {"ok":true,"removed_dir":path|null,"left":{...counts, all 0}}   (refused while running)
  {"cmd":"quit"}
"""
import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from core import ENV, PY, SUMMARIZERS, Session  # noqa: E402
from store import Store  # noqa: E402

A = store = pool = None                        # set by main(); module-level so tests can import handle() without a daemon
session = None
lock = threading.Lock()
docx_lock = threading.Lock()                   # one Word export at a time (each may load its own Gemma)
background = []                                # Lỗi 14: stopped meetings still finishing (notes, MoM, diarization)
quit_ev = threading.Event()


class Pool:
    """Lỗi 15 ("tới lúc đó model mới load à?"): one ASR worker loaded and warmed up BEFORE Record, so a meeting starts in < 1 s.
    Filled when the daemon starts and again REFILL_S after a meeting took it. 05/10: no warm Gemma any more (8.4 GB held all
    day for a MoM after Stop); the meeting loads one on demand (core.Session._ensure_llm)."""
    REFILL_S = 30                                  # let the new meeting's first seconds have the GPU

    def __init__(self):
        self.lock, self.asr, self.n = threading.Lock(), None, 0
        self.run = os.path.realpath(os.path.join(HERE, "..", "run"))
        for f in os.listdir(self.run):               # flags / sockets left by a daemon that was killed (not by a live one: tests run
            if f.startswith("warm_asr_") and f.endswith((".ready", ".sock")):   # next to the user's Middy)
                try:
                    os.kill(int(f.split("_")[2]), 0)
                except ProcessLookupError:
                    os.remove(os.path.join(self.run, f))
                except (ValueError, PermissionError):
                    pass

    def fill(self):
        with self.lock:
            if self.asr is None or self.asr[0].poll() is not None:
                self.n += 1; flag = os.path.join(self.run, f"warm_asr_{os.getpid()}_{self.n}.ready")
                p = subprocess.Popen([PY, os.path.join(HERE, "asr_worker.py"), "--warm", flag], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(os.path.join(self.run, "warm_asr.err"), "a"), env=ENV, bufsize=0)
                self.asr = (p, flag)

    def take(self, kind):
        """(Popen, flag file | socket path), or None -> the meeting starts a cold worker."""
        with self.lock:
            w = getattr(self, kind); setattr(self, kind, None)
        if w and w[0].poll() is not None:            # died while waiting (killed, crashed): forget it and its file
            try:
                os.remove(w[1])
            except OSError:
                pass
            return None
        return w

    def status(self):
        with self.lock:
            a = self.asr
        return {"asr": "none" if not a else "dead" if a[0].poll() is not None else "warm" if os.path.exists(a[1]) else "loading", "llm": "on demand"}

    def close(self):
        with self.lock:
            w, self.asr = self.asr, None
            if w and w[0].poll() is None:
                w[0].kill()
            if w and os.path.exists(w[1]):
                os.remove(w[1])


def rmtree_meeting_dir(m, run_dir):
    """Remove a deleted meeting's run/ directory; only ever inside run/. Returns the path removed or None."""
    import datetime
    import shutil
    run_root = os.path.realpath(os.path.join(HERE, "..", "run"))
    if not run_dir:                                    # meetings recorded before run_dir was stored: the dir is named after the UTC start minute
        guess = os.path.join(run_root, "meeting " + datetime.datetime.fromtimestamp(m["started_at"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M"))
        run_dir = guess if os.path.isdir(guess) else None
    if run_dir and os.path.realpath(run_dir).startswith(run_root + os.sep) and os.path.isdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
        return run_dir
    return None


def running():
    return [s for s in [session] + background if s and s.state != "done"]


def run_session(s):
    """Session.run in its thread; a failure is written to the meeting's status (shown in the library), never swallowed."""
    try:
        s.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        if s.meeting_id:
            store.set_status(s.meeting_id, f"error: {type(e).__name__}: {e}"[:200])
        s.cleanup(); s.state = "done"
        s.publish({"type": "done", "meeting_id": s.meeting_id, "error": str(e)[:200]})


def handle(req):
    global session
    cmd = req.get("cmd")
    if cmd == "start":
        with lock:
            if session and session.state in ("init", "recording") and not session.stop_source.is_set():
                return {"ok": False, "error": "a meeting is already running"}
            if session and session.state != "done":             # stopped but still finishing: it goes on in the background
                session.on_pause = None; background.append(session)
            background[:] = [b for b in background if b.state != "done"]
            olds = list(background)
            session = Session(pool=pool, name=req.get("name", "meeting"), input=req.get("file"), live=req.get("live") if req.get("live") == "ui" else bool(req.get("live")), start=req.get("start", 0.0),
                              end=req.get("end"), language=req.get("language", "English"), space=req.get("space", "default"),
                              num_speakers=req.get("num_speakers", -1), chunk_min=req.get("chunk_min", 10.0), db=A.db, resume=req.get("resume"),
                              no_slides=bool(req.get("no_slides", False)), no_llm=bool(req.get("no_llm", False)),
                              summarizer=req.get("summarizer") if req.get("summarizer") in SUMMARIZERS else "local")
            session.on_pause = lambda p: [b._set_pause(ext=p) for b in olds]    # the new meeting's ASR lag pauses the old Gemma
            threading.Thread(target=run_session, args=(session,), daemon=True).start()
            t = threading.Timer(Pool.REFILL_S, pool.fill); t.daemon = True; t.start()
            return {"ok": True, "meeting_id": None, "note": "meeting_id is in the 'ready' event / status"}
    if cmd == "audio":                         # hot path: no reply
        src = getattr(session, "source", None) if session else None
        if src is not None and hasattr(src, "feed"):
            src.feed(int(req.get("stream", 1)), base64.b64decode(req["pcm"]), req.get("t"))
        return None
    if cmd == "snapshot":                      # full transcript window (client resync after a dropped backlog)
        return {"ok": True, "snapshot": session.tx.snapshot() if session else None}
    if cmd == "meeting":
        return {"ok": True, "meeting": store.meeting(req["meeting_id"]), "notes": store.notes(req["meeting_id"]), "segments": store.segments(req["meeting_id"])}
    if cmd == "set_name":
        store.set_name(req["meeting_id"], req["name"]); return {"ok": True}
    if cmd == "set_space":
        store.set_space(req["meeting_id"], req["space"]); return {"ok": True}
    if cmd == "save_note":
        store.set_note(req["meeting_id"], "user", 0, req["text"]); return {"ok": True}
    if cmd in ("delete_meeting", "delete_meetings"):   # bug 2 (27/09) one meeting; Việc 18 (30/09) many: SQLite rows + run/ files
        ids = [int(req["meeting_id"])] if cmd == "delete_meeting" else [int(i) for i in req.get("meeting_ids", [])]
        busy = {s.meeting_id for s in running()}      # recording, or writing its MoM in the background: skipped, not the whole batch
        metas = {mid: store.meeting(mid) for mid in ids}
        todo = [mid for mid in ids if mid not in busy and metas[mid]]
        dirs = store.delete_meetings(todo)
        removed = {mid: rmtree_meeting_dir(metas[mid], dirs[mid]) for mid in todo}
        skipped, unknown = [mid for mid in ids if mid in busy], [mid for mid in ids if mid not in busy and not metas[mid]]
        if cmd == "delete_meeting":                   # the one-meeting reply the app already uses
            if skipped:
                return {"ok": False, "error": "meeting is running"}
            if unknown:
                return {"ok": False, "error": "unknown meeting"}
            return {"ok": True, "removed_dir": removed[ids[0]], "left": store.counts(ids[0])}
        left = [store.counts(mid) for mid in todo]      # once per meeting (was 4x: one counts() per key)
        return {"ok": True, "deleted": todo, "skipped_running": skipped, "unknown": unknown, "removed_dirs": sum(map(bool, removed.values())),
                "left": {k: sum(c[k] for c in left) for k in ("segments", "notes", "slides", "meetings")}}
    if cmd == "export_docx":                   # Việc 13: fill the company Word MoM from the meeting's notes
        import docx_mom
        mid = int(req["meeting_id"])
        form = req.get("form") or docx_mom.saved_form(store, mid)     # Claude Code's form => no Gemma
        if not form and req.get("summarizer") == "claude":
            return {"ok": False, "error": "Claude mode: ask Claude Code to write the minutes first (Middy MCP: save_minutes with a form)"}
        if not form and running():              # a recording or a finishing meeting holds the GPU; Gemma would be a second load
            return {"ok": False, "error": "a meeting is running"}
        if not docx_lock.acquire(blocking=False):
            return {"ok": False, "error": "a Word export is already running"}
        try:
            return docx_mom.export(store, mid, req["path"], form=form)
        except Exception as e:                  # the reply must always come back (the app waits on this connection)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            docx_lock.release()
    if cmd == "save_minutes":                  # 05/10: minutes written by Claude Code (claude_mcp/mcp_server.py) become the MoM
        import docx_mom                         # the app shows and the Word export fills (form = the Word fields, optional)
        mid, text, form = int(req["meeting_id"]), req.get("text"), req.get("form")
        m = store.meeting(mid)
        if not m:
            return {"ok": False, "error": "unknown meeting"}
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "empty minutes"}
        if form is not None and not (isinstance(form, dict) and (form := docx_mom.parse_reply(json.dumps(form, ensure_ascii=False)))):
            return {"ok": False, "error": "form must be an object (title, objective, highlights, actions, ...)"}
        store.set_note(mid, "mom", 0, text, {"by": "claude", **({"form": form} if form else {})})
        if text.startswith("# ") and re.match(r"(Untitled Note|meeting \d)", m["name"] or ""):     # same rule as the Gemma MoM title
            store.set_name(mid, text.split("\n")[0][2:].strip()[:120])
        return {"ok": True, "chars": len(text), "form": bool(form)}
    if cmd == "ask":                           # Việc 17: "Ask anything" in the overlay; the answer streams as ask_delta / ask_done events
        if not session:
            return {"ok": False, "error": "no meeting"}
        return session.ask(req.get("question", ""))
    if cmd == "set_language":                  # Lỗi 10: change the recognition language mid-meeting (new utterances only)
        if not session or session.state not in ("init", "recording"):   # init = models still loading
            return {"ok": False, "error": "no meeting"}
        return session.set_language(req.get("language", ""))
    if cmd == "spaces":
        return {"ok": True, "spaces": store.spaces()}
    if cmd == "stop":
        if not session:
            return {"ok": False, "error": "no meeting"}
        session.request_stop(); return {"ok": True}
    if cmd == "status":
        st = session.status() if session else {"state": "idle"}
        st["pool"] = pool.status()
        st["background"] = [{"meeting_id": b.meeting_id, "state": b.state} for b in background if b.state != "done"]
        return {"ok": True, "status": st}
    if cmd == "transcript":
        return {"ok": True, "segments": store.segments(req["meeting_id"], req.get("since", 0))}
    if cmd == "notes":
        return {"ok": True, "notes": store.notes(req["meeting_id"])}
    if cmd == "meetings":
        return {"ok": True, "meetings": store.meetings()}
    if cmd == "search":
        return {"ok": True, "hits": store.search(req["q"])}
    if cmd == "glossary_add":
        store.glossary_add(req.get("space", "*"), req["kind"], req["wrong"], req.get("right", "")); return {"ok": True}
    if cmd == "glossary":
        return {"ok": True, "glossary": store.glossary(req.get("space", "default"))}
    if cmd == "quit":
        quit_ev.set(); return {"ok": True}
    return {"ok": False, "error": f"unknown cmd {cmd}"}


def reply(req):
    """handle() for one request. A failing request gets an error reply and the connection stays open: an exception used to end
    the client thread, so one bad request (e.g. a search the FTS5 syntax rejects) closed the app's shared command connection.
    Audio frames never get a reply (nobody reads the audio connection); a bad frame is dropped."""
    if not isinstance(req, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    try:
        return handle(req)
    except Exception as e:
        return None if req.get("cmd") == "audio" else {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def client(conn):
    r, w = conn.makefile("r", encoding="utf-8"), conn.makefile("w", encoding="utf-8")
    try:
        for line in r:
            try:
                req = json.loads(line)
            except ValueError:
                w.write(json.dumps({"ok": False, "error": "invalid JSON"}) + "\n"); w.flush(); continue
            if isinstance(req, dict) and req.get("cmd") == "subscribe":
                s = session
                if not s:
                    w.write(json.dumps({"ok": False, "error": "no meeting"}) + "\n"); w.flush(); continue
                q = s.subscribe()
                try:
                    w.write(json.dumps({"ok": True, "subscribed": True}) + "\n"); w.flush()
                    while True:
                        ev = q.get()
                        w.write(json.dumps(ev, ensure_ascii=False) + "\n"); w.flush()
                        if ev.get("type") == "done":
                            break
                finally:                                # a client that left must not keep a queue the session fills forever
                    if q in s.subscribers:
                        s.subscribers.remove(q)
                continue
            rep = reply(req)
            if rep is not None:
                w.write(json.dumps(rep, ensure_ascii=False) + "\n"); w.flush()
            if quit_ev.is_set():
                break
    except (BrokenPipeError, ConnectionResetError, ValueError):
        pass
    finally:
        conn.close()


def main():
    global A, store, pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=os.path.join(HERE, "..", "run", "midy.sock"))
    ap.add_argument("--db", default=os.path.join(HERE, "..", "run", "midy.db"))
    ap.add_argument("--no-sandbox", action="store_true")
    A = ap.parse_args()
    if not A.no_sandbox and os.environ.get("MIDY_SANDBOX") != "1":
        os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", os.path.join(HERE, "nonet.sb"), "-D", "RUN=" + os.path.realpath(os.path.join(HERE, "..", "run")), sys.executable] + sys.argv, dict(os.environ, MIDY_SANDBOX="1"))
    store = Store(A.db)
    for m in store.meetings():                     # Lỗi 14: nothing runs yet, so these were cut off by a quit/crash mid-way: say so
        if m["status"] in ("recording", "finishing"):
            store.set_status(m["id"], "error: interrupted (Middy quit before the notes were finished)")
    pool = Pool()
    pool.fill()
    if os.path.exists(A.socket):
        os.unlink(A.socket)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(A.socket); srv.listen(4); srv.settimeout(1.0)
    print(json.dumps({"midyd": "ready", "socket": A.socket, "sandbox": os.environ.get("MIDY_SANDBOX") == "1"}), flush=True)
    while not quit_ev.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=client, args=(conn,), daemon=True).start()
    pool.close()
    for s in running():
        s.request_stop()
    srv.close(); os.unlink(A.socket)


if __name__ == "__main__":
    main()
