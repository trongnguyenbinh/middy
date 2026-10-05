"""The daemon's request handling (midyd.handle / reply / client) with a real SQLite store and no workers."""
import json
import os
import queue
import socket
import threading
import types

import pytest

import midyd
from store import Store


class FakeSession:
    def __init__(self, meeting_id, state="recording"):
        self.meeting_id, self.state, self.subscribers = meeting_id, state, []
        self.stop_source = threading.Event()
        self.on_pause = None

    def subscribe(self):
        q = queue.Queue(); self.subscribers.append(q); return q

    def status(self):
        return {"state": self.state, "meeting_id": self.meeting_id}

    def request_stop(self):
        self.stop_source.set()


class FakePool:
    def status(self):
        return {"asr": "warm", "llm": "warm"}


@pytest.fixture
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(midyd, "store", Store(str(tmp_path / "t.db")))
    monkeypatch.setattr(midyd, "pool", FakePool())
    monkeypatch.setattr(midyd, "A", types.SimpleNamespace(db=str(tmp_path / "t.db")))
    monkeypatch.setattr(midyd, "session", None)
    monkeypatch.setattr(midyd, "background", [])
    monkeypatch.setattr(midyd, "HERE", str(tmp_path / "proto"))          # run/ = tmp_path/run for rmtree_meeting_dir
    (tmp_path / "run").mkdir()
    midyd.quit_ev.clear()
    return midyd


def meeting(d, name="m", text="goods receipt MIGO", run_dir=None):
    mid = d.store.new_meeting(name, "default", "English", {"run_dir": run_dir} if run_dir else {})
    d.store.add_segment(mid, {"id": 1, "s": 0.0, "e": 3.0, "text": text, "dropped_lang": False})
    d.store.set_note(mid, "mom", 0, "# MoM")
    return mid


def test_read_commands(d):
    mid = meeting(d)
    assert d.handle({"cmd": "meetings"})["meetings"][0]["id"] == mid
    r = d.handle({"cmd": "meeting", "meeting_id": mid})
    assert r["meeting"]["name"] == "m" and r["notes"][0]["kind"] == "mom" and r["segments"][0]["text"] == "goods receipt MIGO"
    assert d.handle({"cmd": "transcript", "meeting_id": mid, "since": 0})["segments"][0]["seq"] == 1
    assert d.handle({"cmd": "notes", "meeting_id": mid})["notes"][0]["text"] == "# MoM"
    assert d.handle({"cmd": "search", "q": "migo"})["hits"][0]["meeting_id"] == mid
    assert d.handle({"cmd": "spaces"}) == {"ok": True, "spaces": ["default"]}
    assert d.handle({"cmd": "snapshot"}) == {"ok": True, "snapshot": None}


def test_write_commands(d):
    mid = meeting(d)
    assert d.handle({"cmd": "set_name", "meeting_id": mid, "name": "Kế hoạch"}) == {"ok": True}
    assert d.handle({"cmd": "set_space", "meeting_id": mid, "space": "sap"}) == {"ok": True}
    assert d.handle({"cmd": "save_note", "meeting_id": mid, "text": "my note"}) == {"ok": True}
    m = d.store.meeting(mid)
    assert (m["name"], m["space"]) == ("Kế hoạch", "sap") and d.store.notes(mid, "user")[0]["text"] == "my note"
    assert d.handle({"cmd": "glossary_add", "space": "sap", "kind": "term", "wrong": "MIGO"}) == {"ok": True}
    assert d.handle({"cmd": "glossary", "space": "sap"})["glossary"]["terms"] == ["MIGO"]


def test_no_meeting_answers(d):
    for cmd in ("stop", "ask", "set_language"):
        assert d.handle({"cmd": cmd}) == {"ok": False, "error": "no meeting"}
    assert d.handle({"cmd": "status"})["status"] == {"state": "idle", "pool": {"asr": "warm", "llm": "warm"}, "background": []}
    assert d.handle({"cmd": "nope"}) == {"ok": False, "error": "unknown cmd nope"}
    assert d.handle({"cmd": "audio", "stream": 1, "pcm": ""}) is None


def test_start_refused_while_recording(d, monkeypatch):
    monkeypatch.setattr(d, "session", FakeSession(1))
    assert d.handle({"cmd": "start"}) == {"ok": False, "error": "a meeting is already running"}


def test_stop_and_status_with_a_meeting(d, monkeypatch):
    s = FakeSession(7); bg = FakeSession(6, "finishing")
    monkeypatch.setattr(d, "session", s); d.background.append(bg)
    assert d.handle({"cmd": "stop"}) == {"ok": True} and s.stop_source.is_set()
    st = d.handle({"cmd": "status"})["status"]
    assert st["meeting_id"] == 7 and st["background"] == [{"meeting_id": 6, "state": "finishing"}]


def test_delete_meetings_skips_running_and_unknown(d, monkeypatch, tmp_path):
    keep_dir = tmp_path / "run" / "meeting a"; keep_dir.mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    a = meeting(d, "a", run_dir=str(keep_dir))
    b = meeting(d, "b", run_dir=str(outside))                       # a run_dir outside run/ is never removed
    busy = meeting(d, "busy")
    monkeypatch.setattr(d, "session", FakeSession(busy))
    r = d.handle({"cmd": "delete_meetings", "meeting_ids": [a, b, busy, 999]})
    assert (r["deleted"], r["skipped_running"], r["unknown"], r["removed_dirs"]) == ([a, b], [busy], [999], 1)
    assert r["left"] == {"segments": 0, "notes": 0, "slides": 0, "meetings": 0}
    assert not keep_dir.exists() and outside.exists() and d.store.meeting(busy)


def test_delete_meeting_single(d, monkeypatch):
    a = meeting(d, "a"); busy = meeting(d, "busy")
    monkeypatch.setattr(d, "session", FakeSession(busy))
    assert d.handle({"cmd": "delete_meeting", "meeting_id": busy}) == {"ok": False, "error": "meeting is running"}
    assert d.handle({"cmd": "delete_meeting", "meeting_id": 999}) == {"ok": False, "error": "unknown meeting"}
    r = d.handle({"cmd": "delete_meeting", "meeting_id": a})
    assert r["ok"] and r["removed_dir"] is None and set(r["left"].values()) == {0}


def test_rmtree_meeting_dir_guesses_old_layout(d, tmp_path):
    guess = tmp_path / "run" / "meeting 1970-01-01T00:01"; guess.mkdir()
    assert d.rmtree_meeting_dir({"started_at": 60}, None) == str(guess) and not guess.exists()
    assert d.rmtree_meeting_dir({"started_at": 60}, None) is None
    assert d.rmtree_meeting_dir({"started_at": 0}, str(tmp_path / "run" / ".." / "run")) is None   # run/ itself, never


def test_export_docx_refused_while_running_or_busy(d, monkeypatch):
    monkeypatch.setattr(d, "session", FakeSession(1))
    assert d.handle({"cmd": "export_docx", "meeting_id": 1, "path": "/tmp/x.docx"})["error"] == "a meeting is running"
    monkeypatch.setattr(d, "session", None)
    assert d.docx_lock.acquire(blocking=False)
    try:
        assert d.handle({"cmd": "export_docx", "meeting_id": 1, "path": "/tmp/x.docx"})["error"] == "a Word export is already running"
    finally:
        d.docx_lock.release()
    r = d.handle({"cmd": "export_docx", "meeting_id": 999, "path": "/tmp/x.docx"})
    assert r == {"ok": False, "error": "unknown meeting"} and d.docx_lock.acquire(blocking=False)
    d.docx_lock.release()


def test_reply_turns_exceptions_into_error_replies(d):
    assert d.reply({"cmd": "meeting"})["error"].startswith("KeyError")              # missing meeting_id
    assert d.reply([1, 2]) == {"ok": False, "error": "request must be a JSON object"}
    assert d.reply({"cmd": "audio", "pcm": "%%%"}) is None                           # audio: never a reply


def converse(d, lines, n_replies):
    a, b = socket.socketpair()
    t = threading.Thread(target=d.client, args=(b,), daemon=True); t.start()
    a.sendall("".join(lines).encode())
    f = a.makefile("r", encoding="utf-8")
    out = [json.loads(f.readline()) for _ in range(n_replies)]
    f.close(); a.close(); t.join(2)
    return out


def test_client_keeps_the_connection_after_a_bad_request(d):
    mid = meeting(d)
    out = converse(d, ["not json\n", '{"cmd":"search","q":"SAP-MM\\"("}\n', '{"cmd":"meeting"}\n', "[]\n",
                       json.dumps({"cmd": "meeting", "meeting_id": mid}) + "\n"], 5)
    assert out[0] == {"ok": False, "error": "invalid JSON"}
    assert out[1] == {"ok": True, "hits": []}
    assert out[2]["ok"] is False and out[3]["ok"] is False
    assert out[4]["meeting"]["id"] == mid                                         # still answering on the same connection


def test_client_replies_utf8(d):
    meeting(d, "Họp kế hoạch")
    assert converse(d, ['{"cmd":"meetings"}\n'], 1)[0]["meetings"][0]["name"] == "Họp kế hoạch"


def test_subscribe_streams_until_done_and_unsubscribes(d, monkeypatch):
    s = FakeSession(3); monkeypatch.setattr(d, "session", s)
    a, b = socket.socketpair()
    t = threading.Thread(target=d.client, args=(b,), daemon=True); t.start()
    a.sendall(b'{"cmd":"subscribe"}\n')
    f = a.makefile("r", encoding="utf-8")
    assert json.loads(f.readline()) == {"ok": True, "subscribed": True}
    q = s.subscribers[0]
    q.put({"type": "note", "text": "ghi chú"}); q.put({"type": "done"})
    assert [json.loads(f.readline())["type"] for _ in range(2)] == ["note", "done"]
    a.sendall(b'{"cmd":"status"}\n')                                               # back to request mode
    assert json.loads(f.readline())["status"]["meeting_id"] == 3
    f.close(); a.close(); t.join(2)
    assert s.subscribers == []


def test_subscriber_that_leaves_is_removed(d, monkeypatch):
    s = FakeSession(3); monkeypatch.setattr(d, "session", s)
    a, b = socket.socketpair()
    t = threading.Thread(target=d.client, args=(b,), daemon=True); t.start()
    a.sendall(b'{"cmd":"subscribe"}\n'); a.makefile("r").readline()
    a.close()
    s.subscribers[0].put({"type": "x" * 200000})                                  # write fails: the client is gone
    t.join(2)
    assert not t.is_alive() and s.subscribers == []


def test_subscribe_without_meeting(d):
    assert converse(d, ['{"cmd":"subscribe"}\n'], 1) == [{"ok": False, "error": "no meeting"}]


def test_quit_sets_the_event(d):
    assert converse(d, ['{"cmd":"quit"}\n'], 1) == [{"ok": True}] and d.quit_ev.is_set()
    d.quit_ev.clear()


def test_run_session_records_the_error(d):
    mid = meeting(d)
    events = []
    s = types.SimpleNamespace(meeting_id=mid, state="recording", cleaned=False)
    s.run = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    s.cleanup = lambda: setattr(s, "cleaned", True)
    s.publish = events.append
    d.run_session(s)
    assert d.store.meeting(mid)["status"] == "error: RuntimeError: boom" and s.cleaned and s.state == "done"
    assert events == [{"type": "done", "meeting_id": mid, "error": "boom"}]


def test_docstring_matches_start_reply():
    assert '"meeting_id":null' in midyd.__doc__
    assert os.path.basename(midyd.__file__) == "midyd.py"


def test_save_minutes_from_claude(d):
    mid = d.store.new_meeting("meeting 2026-10-05T09:00", "default", "Vietnamese", {})
    assert d.handle({"cmd": "save_minutes", "meeting_id": 999, "text": "# x"}) == {"ok": False, "error": "unknown meeting"}
    assert d.handle({"cmd": "save_minutes", "meeting_id": mid, "text": "  "}) == {"ok": False, "error": "empty minutes"}
    assert d.handle({"cmd": "save_minutes", "meeting_id": mid, "text": "# x", "form": "nope"})["ok"] is False
    r = d.handle({"cmd": "save_minutes", "meeting_id": mid, "text": "# Họp kho\n- ý", "form": {"title": "Họp kho", "actions": [{"action": "Gửi file", "owner": "Lan"}]}})
    assert r == {"ok": True, "chars": 13, "form": True}
    n = d.store.notes(mid, "mom")[0]
    assert n["text"].startswith("# Họp kho") and n["stats"]["by"] == "claude" and n["stats"]["form"]["actions"] == [{"action": "Gửi file", "owner": "Lan", "date": ""}]
    assert d.store.meeting(mid)["name"] == "Họp kho"                       # default name replaced by the minutes' title


def test_export_docx_claude_mode(d, monkeypatch):
    import docx_mom
    mid = meeting(d)
    r = d.handle({"cmd": "export_docx", "meeting_id": mid, "path": "/tmp/x.docx", "summarizer": "claude"})
    assert r["ok"] is False and "Claude mode" in r["error"]                     # no form yet, and Gemma is not allowed
    calls = []
    monkeypatch.setattr(docx_mom, "export", lambda store, m, path, form=None: calls.append(form) or {"ok": True})
    monkeypatch.setattr(d, "session", FakeSession(mid + 1))                      # a meeting is recording ...
    assert d.handle({"cmd": "export_docx", "meeting_id": mid, "path": "/tmp/x.docx", "form": {"title": "t"}}) == {"ok": True}
    assert calls == [{"title": "t"}]                                             # ... but a form needs no Gemma, so it runs


def test_start_passes_summarizer(d, monkeypatch):
    got = []
    class S(FakeSession):
        def __init__(self, **cfg):
            super().__init__(None, "init"); got.append(cfg)
        def run(self):
            pass
    monkeypatch.setattr(d, "Session", S)
    monkeypatch.setattr(d, "pool", types.SimpleNamespace(fill=lambda: None, status=lambda: {}))
    monkeypatch.setattr(d.Pool, "REFILL_S", 3600)
    d.handle({"cmd": "start", "summarizer": "claude"})
    d.session.state = "done"
    d.handle({"cmd": "start", "summarizer": "bogus"})
    assert [c["summarizer"] for c in got] == ["claude", "local"]
