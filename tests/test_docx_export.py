"""Word MoM export end to end with a fake Gemma: template filled, labels translated, highlights from the MoM parts,
retries, and the guard that refuses to leave template text behind."""
import datetime
import json
import re
import zipfile

import pytest

import docx_mom
from store import Store

MOM = """# Họp nhập kho

## Tóm tắt
Bàn về nhập kho.

## Nội dung chính
### [00:00–09:50]
* Tạo PO bằng ME21N
* Nhập kho bằng MIGO
### [10:00–12:00]
* Chị Lan gửi file

## Việc cần làm
| Ai | Việc | Khi nào |
"""
REPLY = {"title": "Họp nhập kho tháng mười", "location": "Phòng họp 2", "organizer": "Anh Hải", "invitees": "Lan, Vinh",
         "objective": ["Thống nhất quy trình nhập kho"], "highlights": [{"notes": "Tạo đơn mua hàng", "action": "", "target": ""}],
         "actions": [{"action": "Gửi file mẫu", "owner": "Chị Lan", "date": "Thứ sáu"}, {"action": "", "owner": "x", "date": ""}]}


@pytest.fixture
def st(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    st.mid = st.new_meeting("meeting 1", "default", "Vietnamese", {})
    st.add_segment(st.mid, {"id": 1, "s": 65.0, "e": 70.0, "text": "đơn mua hàng tạo bằng ME21N cho nhà cung cấp", "dropped_lang": False})
    return st


def fake_gemma(replies):
    calls = []

    def ask(prompt, run_root):
        calls.append(prompt)
        text = replies[min(len(calls), len(replies)) - 1]
        return {"text": text, "stats": {"finish": "stop", "gen_tokens": 9, "wall_s": 1.0}}
    ask.calls = calls
    return ask


def doc_texts(path):
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode()
        hdr = z.read("word/header1.xml").decode()
    return [docx_mom.ptext(p).strip() for p in docx_mom.P_RE.findall(doc) if docx_mom.ptext(p).strip()], docx_mom.ptext(hdr)


def test_export_from_a_parts_mom(st, tmp_path, monkeypatch):
    st.set_note(st.mid, "mom", 0, MOM)
    ask = fake_gemma(["Đây là JSON:\n" + json.dumps(REPLY, ensure_ascii=False)])
    monkeypatch.setattr(docx_mom, "ask_gemma", ask)
    out = str(tmp_path / "m.docx")
    now = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.timezone.utc)
    r = docx_mom.export(st, st.mid, out, run_root=str(tmp_path), now=now)
    assert r["ok"] and r["language"] == "Vietnamese" and r["highlights_from_parts"] and r["highlights"] == 2 and r["highlight_points"] == 3
    assert r["actions"] == 1 and r["tries"] == 1 and r["warn"] == []
    assert "ME21N" not in ask.calls[0] and "Bàn về nhập kho" in ask.calls[0]     # Main content (the parts) is not sent back to Gemma
    texts, hdr = doc_texts(out)
    assert "Biên bản cuộc họp" in hdr
    for t in ("Thông tin cuộc họp", "Họp nhập kho tháng mười", "Phòng họp 2", "[00:00–09:50]", "Tạo PO bằng ME21N", "Gửi file mẫu", "Chị Lan"):
        assert t in texts, t
    assert "Thống nhất quy trình nhập kho" in texts
    with zipfile.ZipFile(out) as z:
        core_xml = z.read("docProps/core.xml").decode()
    assert "<dc:creator>Middy</dc:creator>" in core_xml and "2026-10-05T09:00:00Z" in core_xml
    assert oct(__import__("os").stat(out).st_mode & 0o777) == "0o600"


def test_export_old_mom_uses_gemma_highlights_with_transcript_times(st, tmp_path, monkeypatch):
    st.set_note(st.mid, "live", 0, "- tạo đơn mua hàng")
    reply = dict(REPLY, highlights=[{"notes": "Đơn mua hàng tạo bằng ME21N cho nhà cung cấp", "action": "", "target": ""}])
    monkeypatch.setattr(docx_mom, "ask_gemma", fake_gemma([json.dumps(reply, ensure_ascii=False)]))
    out = str(tmp_path / "m.docx")
    r = docx_mom.export(st, st.mid, out, run_root=str(tmp_path))
    assert r["ok"] and not r["highlights_from_parts"]
    texts, _ = doc_texts(out)
    assert "[01:05] Đơn mua hàng tạo bằng ME21N cho nhà cung cấp" in texts


def test_export_user_note_wins_and_retry_once(st, tmp_path, monkeypatch):
    st.set_note(st.mid, "mom", 0, MOM); st.set_note(st.mid, "user", 0, "# Ghi chú của tôi\n- một ý")
    ask = fake_gemma(["not json", json.dumps(REPLY, ensure_ascii=False)])
    monkeypatch.setattr(docx_mom, "ask_gemma", ask)
    r = docx_mom.export(st, st.mid, str(tmp_path / "m.docx"), run_root=str(tmp_path))
    assert r["ok"] and r["tries"] == 2 and "Ghi chú của tôi" in ask.calls[0] and not r["highlights_from_parts"]


def test_export_errors(st, tmp_path, monkeypatch):
    assert docx_mom.export(st, 999, "x.docx") == {"ok": False, "error": "unknown meeting"}
    assert docx_mom.export(st, st.mid, "x.docx") == {"ok": False, "error": "this meeting has no notes"}
    st.set_note(st.mid, "mom", 0, MOM)
    monkeypatch.setattr(docx_mom, "ask_gemma", fake_gemma(["no", "still no"]))
    assert docx_mom.export(st, st.mid, str(tmp_path / "m.docx"), run_root=str(tmp_path)) == {"ok": False, "error": "the local model did not return the form", "tries": 2}
    assert not (tmp_path / "m.docx").exists()


def test_english_meeting_keeps_english_labels(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "t.db"))
    mid = st.new_meeting("weekly", "default", "English", {})
    st.add_segment(mid, {"id": 1, "s": 0.0, "e": 4.0, "text": "we post the goods receipt today", "dropped_lang": False})
    st.set_note(mid, "mom", 0, "# Weekly\n## Summary\nx\n## Main content\n### [00:00–00:04]\n* goods receipt\n")
    reply = dict(REPLY, title="Weekly sync", objective=[], actions=[])
    monkeypatch.setattr(docx_mom, "ask_gemma", fake_gemma([json.dumps(reply)]))
    out = str(tmp_path / "e.docx")
    assert docx_mom.export(st, mid, out, run_root=str(tmp_path))["language"] == "English"
    texts, hdr = doc_texts(out)
    assert "Minutes of Meeting" in hdr and "Meeting Details" in texts and "N/A" in texts


def test_write_docx_refuses_text_it_was_not_given(tmp_path):
    vals = {"details": {"Meeting Title:": "T"}, "objective": ["o"], "highlights": [["1", "h", "", ""]], "actions": [["1", "a", "", ""]],
            "allowed": {"T", "o", "1", "a"}}                                      # "h" missing from allowed
    out = tmp_path / "x.docx"
    with pytest.raises(ValueError, match="template text left"):
        docx_mom.write_docx(str(out), lambda t: t, vals, datetime.datetime.now().astimezone())
    assert not out.exists()


def test_parse_reply():
    assert docx_mom.parse_reply("no braces") is None and docx_mom.parse_reply("{broken") is None
    j = docx_mom.parse_reply('```json\n{"title": " A\\n B ", "objective": ["", "x"], "highlights": [1, {"notes": ""}, {"notes": "n"}], "actions": null}\n```')
    assert j["title"] == "A B" and j["objective"] == ["x"] and j["highlights"] == [{"notes": "n", "action": "", "target": ""}] and j["actions"] == []


def test_meeting_when():
    ts = datetime.datetime(2026, 10, 5, 9, 30).timestamp()
    assert docx_mom.meeting_when({"started_at": ts, "audio_end_s": 3600}, "Vietnamese") == "05/10/2026, 09:30–10:30"
    assert docx_mom.meeting_when({"started_at": ts}, "English") == "5 October 2026, 09:30"


def test_set_text_escapes_and_empties_extra_runs():
    p = '<w:p><w:r><w:t>old</w:t></w:r><w:r><w:t xml:space="preserve"> more</w:t></w:r></w:p>'
    out = docx_mom.set_text(p, "a & <b>")
    assert docx_mom.ptext(out) == "a & <b>" and out.count("<w:t") == 2 and re.search(r"<w:t></w:t>", out)
    assert docx_mom.set_text("<w:p/>", "x") == "<w:p/>"


def test_export_with_claude_form_needs_no_gemma(st, tmp_path, monkeypatch):
    st.set_note(st.mid, "mom", 0, MOM, {"by": "claude", "form": dict(REPLY, highlights=[{"notes": "Ý chính của Claude", "action": "Làm X", "target": "Thứ Hai"}])})
    monkeypatch.setattr(docx_mom, "ask_gemma", lambda *a: pytest.fail("Gemma must not load when Claude gave the form"))
    out = str(tmp_path / "m.docx")
    r = docx_mom.export(st, st.mid, out, run_root=str(tmp_path))
    assert r["ok"] and r["tries"] == 0 and not r["highlights_from_parts"] and r["highlights"] == 1
    texts, _ = doc_texts(out)
    assert "Ý chính của Claude" in texts and "Họp nhập kho tháng mười" in texts
