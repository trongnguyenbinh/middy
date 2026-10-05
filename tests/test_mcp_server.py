"""claude_mcp/mcp_server.py tools against the REAL daemon request handling (midyd.client on a Unix socket, real SQLite store,
no worker, no model). The mcp package is only needed for the registration test."""
import os
import socket
import sys
import tempfile
import threading
import types

import pytest

import midyd
from store import Store

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude_mcp"))
import mcp_server  # noqa: E402


@pytest.fixture
def ms(tmp_path, monkeypatch):
    monkeypatch.setattr(midyd, "store", Store(str(tmp_path / "t.db")))
    monkeypatch.setattr(midyd, "pool", types.SimpleNamespace(status=lambda: {}))
    monkeypatch.setattr(midyd, "session", None)
    monkeypatch.setattr(midyd, "background", [])
    midyd.quit_ev.clear()
    d = tempfile.mkdtemp(prefix="mcp", dir="/tmp")                # short: AF_UNIX paths are limited to ~104 bytes
    sock = os.path.join(d, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock); srv.listen(4)
    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=midyd.client, args=(conn,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(mcp_server, "SOCKET", sock)
    docs = tmp_path / "Documents"; docs.mkdir()
    monkeypatch.setattr(mcp_server, "DOCS", str(docs))
    st = midyd.store
    mid = st.new_meeting("Họp kho", "default", "Vietnamese", {"file": None})
    for i in range(5):
        st.add_segment(mid, {"id": i + 1, "s": 60.0 * i + 5, "e": 60.0 * i + 9, "speaker": f"Speaker {i % 2 + 1}", "text": f"câu số {i} về GRN", "dropped_lang": False})
    st.add_segment(mid, {"id": 6, "s": 400.0, "e": 412.0, "text": "", "dropped_lang": True})
    st.set_note(mid, "live", 0, "- [00:05] Speaker 1: câu số 0", {"light": True})
    yield types.SimpleNamespace(mid=mid, docs=docs, store=st)
    srv.close(); os.remove(sock); os.rmdir(d)


def test_read_tools(ms):
    assert mcp_server.list_meetings()[0] | {} == mcp_server.list_meetings()[0] and mcp_server.list_meetings()[0]["duration"] == "00:06:52"
    m = mcp_server.get_meeting(ms.mid)
    assert m["segments"] == 6 and m["speakers"] == ["Speaker 1", "Speaker 2"] and m["notes"] == [{"kind": "live", "idx": 0, "chars": 29, "by": None}]
    assert "source" not in m
    p1 = mcp_server.get_transcript(ms.mid, limit=4)
    assert p1["total"] == 6 and p1["next_offset"] == 4 and p1["lines"][1] == "[00:01:05] Speaker 2: câu số 1 về GRN"
    p2 = mcp_server.get_transcript(ms.mid, offset=p1["next_offset"], limit=4)
    assert p2["next_offset"] is None and p2["lines"][-1] == "[00:06:40] Speaker ?: [another language, ~12 s, not transcribed]"
    assert mcp_server.search("GRN", limit=2)[0]["meeting_id"] == ms.mid and len(mcp_server.search("GRN", limit=2)) == 2
    assert mcp_server.get_minutes(ms.mid) == {"meeting_id": ms.mid, "source": "live", "by": "light extract", "has_form": False, "text": "- [00:05] Speaker 1: câu số 0"}
    assert mcp_server.get_glossary() == {"corrections": {}, "terms": [], "ambiguous": []}


def test_errors_are_raised(ms, monkeypatch):
    with pytest.raises(RuntimeError, match="unknown meeting"):
        mcp_server.get_meeting(999)
    monkeypatch.setattr(mcp_server, "SOCKET", "/tmp/no-such-midy.sock")
    with pytest.raises(RuntimeError, match="Middy is not running"):
        mcp_server.list_meetings()


def test_save_minutes_then_export_inside_documents_only(ms):
    form = {"title": "Họp kho", "objective": ["Chốt quy trình"], "highlights": [{"notes": "Dùng GRN", "action": "Lan gửi file", "target": "Thứ Hai"}],
            "actions": [{"action": "Gửi file mẫu", "owner": "Lan", "date": "Thứ Hai"}]}
    assert mcp_server.save_minutes(ms.mid, "# Họp kho\n- Dùng GRN", form)["ok"]
    got = mcp_server.get_minutes(ms.mid)
    assert got["source"] == "mom" and got["by"] == "claude" and got["has_form"]
    for bad in ("/tmp/x.docx", "../../x.docx", "x.txt", str(ms.docs / ".." / "x.docx")):
        with pytest.raises(ValueError):
            mcp_server.export_docx(ms.mid, bad)
    r = mcp_server.export_docx(ms.mid)                           # saved form => no local model
    assert r["ok"] and r["tries"] == 0 and r["path"] == os.path.realpath(ms.docs / "Middy" / "Họp kho - MoM.docx")
    assert os.stat(r["path"]).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        mcp_server.export_docx(ms.mid)
    assert mcp_server.export_docx(ms.mid, overwrite=True)["ok"]


def test_export_without_form_is_refused_in_claude_mode(ms):
    mcp_server.save_minutes(ms.mid, "# Họp kho\n- ý")
    with pytest.raises(RuntimeError, match="Claude mode"):
        mcp_server.export_docx(ms.mid)


def test_registered_tools():
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio
    tools = {t.name: t for t in asyncio.run(mcp_server.build().list_tools())}
    assert set(tools) == {"list_meetings", "get_meeting", "get_transcript", "search", "get_minutes", "save_minutes", "export_docx", "get_glossary"}
    ro = {n for n, t in tools.items() if t.annotations.read_only_hint}
    assert ro == set(tools) - {"save_minutes", "export_docx"}
    assert set(tools["get_transcript"].input_schema["properties"]) == {"meeting_id", "offset", "limit"}


def test_refusal_reason_reaches_claude(ms):
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError               # the SDK turns it into an is_error result with this text
    with pytest.raises(ToolError, match="inside ~/Documents"):
        asyncio.run(mcp_server.build().call_tool("export_docx", {"meeting_id": ms.mid, "path": "/tmp/x.docx"}))
