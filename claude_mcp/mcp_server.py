"""Middy for Claude Code: an MCP server (stdio) over the daemon's Unix socket (proto/midyd.py). No network port, no model.
Runs OUTSIDE the no-network sandbox (it is started by Claude Code), but only ever talks to the daemon socket in run/;
meeting text leaves the Mac only when Claude Code calls one of these tools. Read-only except save_minutes and export_docx.
  claude mcp add --scope user middy -- <repo>/.venv-mcp/bin/python <repo>/claude_mcp/mcp_server.py
The Middy app must be running (its daemon serves run/midy.sock; MIDY_SOCKET overrides the path).
"""
import json
import os
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
SOCKET = os.environ.get("MIDY_SOCKET", os.path.join(HERE, "..", "run", "midy.sock"))
DOCS = os.path.expanduser("~/Documents")
TIMEOUT_S = 120
FORM_DOC = ('form (optional, fills the company Word template): {"title", "location", "organizer", "invitees", "objective": [str], '
            '"highlights": [{"notes", "action", "target"}], "actions": [{"action", "owner", "date"}]}')


def daemon(req):
    """One request on its own connection -> the reply dict. Raises when Middy is not running or the daemon refuses."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(TIMEOUT_S)
    try:
        c.connect(SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        c.close()
        raise RuntimeError("Middy is not running: open the Middy app (its daemon serves run/midy.sock)") from None
    with c, c.makefile("rw", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n"); f.flush()
        line = f.readline()
    if not line:
        raise RuntimeError("Middy closed the connection")
    r = json.loads(line)
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "Middy refused the request")
    return r


def hms(s):
    s = int(s or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---- tools (plain functions: tests call them without the mcp package) -------------------------------------------------
def list_meetings(limit: int = 30) -> list:
    """Recorded meetings, newest first: id, name, status, start time (unix s), length, space, language."""
    return [{**m, "duration": hms(m.get("audio_end_s"))} for m in daemon({"cmd": "meetings"})["meetings"][:max(1, limit)]]


def get_meeting(meeting_id: int) -> dict:
    """One meeting: details, which notes exist (minutes / live notes / questions), segment count, speakers. No transcript text."""
    r = daemon({"cmd": "meeting", "meeting_id": meeting_id})
    if not r.get("meeting"):
        raise RuntimeError("unknown meeting")
    segs = r.get("segments") or []
    m = dict(r["meeting"]); m.pop("source", None)
    return {**m, "duration": hms(m.get("audio_end_s")), "segments": len(segs), "speakers": sorted({s["speaker"] for s in segs if s.get("speaker")}),
            "notes": [{"kind": n["kind"], "idx": n["idx"], "chars": len(n["text"]), "by": (n.get("stats") or {}).get("by")} for n in r.get("notes") or []]}


def get_transcript(meeting_id: int, offset: int = 0, limit: int = 200) -> dict:
    """Transcript lines [hh:mm:ss from meeting start] speaker: text, `limit` lines from `offset`. Page with next_offset until it is null."""
    segs = daemon({"cmd": "transcript", "meeting_id": meeting_id, "since": 0})["segments"]
    limit, offset = max(1, min(limit, 1000)), max(0, offset)
    page = segs[offset:offset + limit]
    lines = [f"[{hms(s['s'])}] {s.get('speaker') or 'Speaker ?'}: " + (f"[another language, ~{int(s['e'] - s['s'])} s, not transcribed]" if s.get("dropped_lang") else s["text"])
             for s in page]
    nxt = offset + len(page)
    return {"meeting_id": meeting_id, "total": len(segs), "offset": offset, "next_offset": nxt if nxt < len(segs) else None, "lines": lines}


def search(query: str, limit: int = 20) -> list:
    """Full-text search over every meeting's transcript; hits carry meeting_id, time and speaker (use get_transcript for context)."""
    return [{"meeting_id": h["meeting_id"], "t": hms(h["s"]), "speaker": h["speaker"], "snippet": h["snippet"]}
            for h in daemon({"cmd": "search", "q": query})["hits"][:max(1, limit)]]


def get_minutes(meeting_id: int) -> dict:
    """The meeting's minutes: the user's edited note if any, else the MoM (local model or Claude), else the live notes."""
    notes = daemon({"cmd": "notes", "meeting_id": meeting_id})["notes"]
    for kind in ("user", "mom", "live"):
        ns = sorted((n for n in notes if n["kind"] == kind), key=lambda n: n["idx"])
        if ns:
            st = ns[-1].get("stats") or {}
            return {"meeting_id": meeting_id, "source": kind, "by": st.get("by") or ("light extract" if st.get("light") else "local model" if kind == "mom" else "user" if kind == "user" else None),
                    "has_form": bool(st.get("form")), "text": ns[-1]["text"]}
    return {"meeting_id": meeting_id, "source": None, "text": ""}


def save_minutes(meeting_id: int, markdown: str, form: dict | None = None) -> dict:
    """Save minutes you wrote as the meeting's MoM (shown in the Middy app, used by the Word export). Start with "# <title>".
    Replaces the current MoM of that meeting."""
    return daemon({"cmd": "save_minutes", "meeting_id": meeting_id, "text": markdown, "form": form})


def docx_path(path):
    """The Word file must land inside ~/Documents (default ~/Documents/Middy/). Returns the resolved path or raises."""
    docs = os.path.realpath(DOCS)
    p = os.path.realpath(os.path.expanduser(path if os.path.isabs(os.path.expanduser(path)) else os.path.join(DOCS, "Middy", path)))
    if not p.startswith(docs + os.sep) or not p.lower().endswith(".docx"):
        raise ValueError("path must be a .docx file inside ~/Documents")
    return p


def export_docx(meeting_id: int, path: str = "", form: dict | None = None, overwrite: bool = False) -> dict:
    """Write the meeting's MoM as the company Word file, inside ~/Documents only (default ~/Documents/Middy/<name> - MoM.docx).
    Uses `form`, else the form saved with save_minutes; without either the app's local model would be needed.
    An existing file is kept unless overwrite is true."""
    if not path:
        name = (daemon({"cmd": "meeting", "meeting_id": meeting_id}).get("meeting") or {}).get("name") or f"meeting {meeting_id}"
        path = "".join("_" if c in '\\/:*?"<>|' else c for c in name) + " - MoM.docx"
    p = docx_path(path)
    if os.path.exists(p) and not overwrite:
        raise FileExistsError(f"{p} exists; pass overwrite=true to replace it")
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    return daemon({"cmd": "export_docx", "meeting_id": meeting_id, "path": p, "form": form, "summarizer": "claude"})


def get_glossary(space: str = "default") -> dict:
    """Term glossary of a space (corrections wrong -> right, terms, ambiguous words) the recogniser uses."""
    return daemon({"cmd": "glossary", "space": space})["glossary"]


READ = (list_meetings, get_meeting, get_transcript, search, get_minutes, get_glossary)
WRITE = (save_minutes, export_docx)


def build():
    import functools
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    def told(f):                                 # a refusal (not running, outside ~/Documents, unknown meeting) reaches Claude with its
        @functools.wraps(f)                      # reason; the SDK hides the text of any other exception
        def w(*a, **k):
            try:
                return f(*a, **k)
            except (RuntimeError, ValueError, OSError) as e:
                raise ToolError(str(e)) from None
        return w
    srv = MCPServer("middy", instructions="Meetings recorded and transcribed on this Mac by Middy. To write minutes: get_meeting, read the "
                    "whole transcript with get_transcript (follow next_offset), then save_minutes (markdown starting with '# <title>', in the "
                    "meeting's language) with a form so export_docx can fill the Word template. " + FORM_DOC)
    for f in READ:
        srv.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))(told(f))
    srv.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
             description=save_minutes.__doc__ + " " + FORM_DOC)(told(save_minutes))
    srv.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
             description=export_docx.__doc__ + " " + FORM_DOC)(told(export_docx))
    return srv


if __name__ == "__main__":
    build().run("stdio")
