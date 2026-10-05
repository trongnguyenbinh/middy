"""SQLite schema, FTS5 triggers and search, glossary merge, notes upsert, transactional delete."""
import json

import pytest

from store import Store


@pytest.fixture
def st(tmp_path):
    return Store(str(tmp_path / "sub" / "t.db"))          # the parent directory is created


def seg(i, text, s=0.0, e=1.0, **kw):
    return {"id": i, "s": s, "e": e, "text": text, "dropped_lang": False, **kw}


def test_schema_wal_and_indexes(st):
    assert st.db.execute("pragma journal_mode").fetchone()[0] == "wal"
    idx = {r[0] for r in st.db.execute("select name from sqlite_master where type='index'")}
    assert "slides_meeting" in idx


def test_meeting_rows_and_order(st):
    a = st.new_meeting("a", "ops", "English", {"file": None}, 3)
    b = st.new_meeting("b", "default", "Vietnamese", {"file": "x.mp4"})
    assert [m["id"] for m in st.meetings()] == [b, a]                 # newest first
    m = st.meeting(a)
    assert (m["name"], m["space"], m["status"], m["num_speakers"]) == ("a", "ops", "recording", 3)
    st.set_name(a, "renamed"); st.set_space(a, "x"); st.set_language(a, "German"); st.set_status(a, "done")
    m = st.meeting(a)
    assert (m["name"], m["space"], m["language"], m["status"]) == ("renamed", "x", "German", "done")
    st.set_run_dir(a, "/r/a")
    assert json.loads(st.meeting(a)["source"]) == {"file": None, "run_dir": "/r/a"}
    assert st.spaces() == ["default", "x"]
    assert st.meeting(999) is None


def test_segments_since_speaker_and_audio_end(st):
    m = st.new_meeting("m", "d", "English", {})
    st.add_segment(m, seg(1, "one", 0, 4, stream="system", speaker="Speaker 1"))
    st.add_segment(m, seg(2, "two", 4, 9, stream="mic", speaker="You"))
    st.add_segment(m, seg(2, "dup", 4, 9))                          # same seq: ignored (crash replay)
    assert [s["text"] for s in st.segments(m)] == ["one", "two"]
    assert [s["seq"] for s in st.segments(m, since_seq=1)] == [2]
    assert st.meeting(m)["audio_end_s"] == 9.0
    st.set_speaker(m, 1, "Speaker 3")
    assert st.segments(m)[0]["speaker"] == "Speaker 3"


def test_fts_follows_insert_update_delete(st):
    m = st.new_meeting("m", "d", "English", {})
    st.add_segment(m, seg(1, "budget plan in QR"))
    st.add_segment(m, seg(2, "service review SLA"))
    assert [h["seq"] for h in st.search("qr")] == [1]
    assert st.search("qr")[0]["snippet"] == "budget plan in [QR]"
    st.db.execute("update segments set text='project plan Q3' where meeting_id=? and seq=1", (m,))
    assert st.search("qr") == [] and [h["seq"] for h in st.search("Q3")] == [1]
    st.delete_meeting(m)
    assert st.search("SLA") == []


@pytest.mark.parametrize("q", ['KPI-Q4', '"', 'a"b', "budget AND", "(", "*", "NEAR("])
def test_search_never_raises_on_typed_text(st, q):
    m = st.new_meeting("m", "d", "English", {})
    st.add_segment(m, seg(1, "KPI-Q4 budget plan"))
    assert isinstance(st.search(q), list)


def test_search_falls_back_to_phrases(st):
    m = st.new_meeting("m", "d", "English", {})
    st.add_segment(m, seg(1, "module KPI-Q4 budget plan"))
    assert [h["seq"] for h in st.search("KPI-Q4")] == [1]           # raw FTS5 would read "Q4" as a column name
    assert [h["seq"] for h in st.search("budget OR nothing")] == [1]  # valid FTS5 syntax is still used as such


def test_search_limit(st):
    m = st.new_meeting("m", "d", "English", {})
    for i in range(1, 8):
        st.add_segment(m, seg(i, f"qr line {i}", i, i + 1))
    assert len(st.search("qr", limit=3)) == 3


def test_notes_upsert_and_kind_filter(st):
    m = st.new_meeting("m", "d", "English", {})
    st.set_note(m, "part", 0, "p0"); st.set_note(m, "part", 0, "p0 v2", {"gen_tokens": 5}); st.set_note(m, "mom", 0, "# MoM")
    assert [(n["kind"], n["text"]) for n in st.notes(m)] == [("mom", "# MoM"), ("part", "p0 v2")]
    assert st.notes(m, "part")[0]["stats"] == {"gen_tokens": 5}
    assert st.notes(m, "live") == []


def test_slides(st):
    m = st.new_meeting("m", "d", "English", {})
    st.add_slide(m, 12.0, ["QR"], "Budget plan\nQR"); st.add_slide(m, 3.0, [], "")
    assert [s["t"] for s in st.slides(m)] == [3.0, 12.0] and st.slides(m)[1]["words"] == ["QR"]


def test_glossary_merge_space_and_star(st, tmp_path):
    st.glossary_add("*", "correction", "marketing ab", "Marketing A/B")
    st.glossary_add("ops", "correction", "marketing ab", "Marketing AB (space)")   # the space overrides '*'
    st.glossary_add("ops", "term", "QR"); st.glossary_add("ops", "ambiguous", "cả ba")
    g = st.glossary("ops")
    assert g == {"corrections": {"marketing ab": "Marketing AB (space)"}, "terms": ["QR"], "ambiguous": ["cả ba"]}
    assert st.glossary("other")["corrections"] == {"marketing ab": "Marketing A/B"}
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"corrections": {"quý ba": "Q3"}, "terms": ["SLA"], "ambiguous": ["x"]}))
    st.import_glossary_json("imp", str(p))
    assert st.glossary("imp") == {"corrections": {"marketing ab": "Marketing A/B", "quý ba": "Q3"}, "terms": ["SLA"], "ambiguous": ["x"]}


def test_delete_meetings_counts_and_rollback(st):
    ids = []
    for k in range(3):
        m = st.new_meeting(f"m{k}", "d", "English", {"run_dir": f"/run/m{k}"} if k else {})
        st.add_segment(m, seg(1, f"text {k}")); st.set_note(m, "mom", 0, "x"); st.add_slide(m, 1.0, [], "")
        ids.append(m)
    assert st.counts(ids[0]) == {"segments": 1, "notes": 1, "slides": 1, "fts": 1, "meetings": 1}
    assert st.delete_meetings(ids[:2] + [999]) == {ids[0]: None, ids[1]: "/run/m1", 999: None}
    assert st.counts(ids[0]) == {"segments": 0, "notes": 0, "slides": 0, "fts": 0, "meetings": 0}
    orig = st.delete_meeting
    st.delete_meeting = lambda mid: (_ for _ in ()).throw(RuntimeError("disk"))   # first delete fails -> nothing removed
    with pytest.raises(RuntimeError):
        st.delete_meetings([ids[2]])
    st.delete_meeting = orig
    assert st.counts(ids[2])["meetings"] == 1


def test_reopen_keeps_rows(tmp_path):
    p = str(tmp_path / "t.db")
    a = Store(p); m = a.new_meeting("m", "d", "English", {}); a.add_segment(m, seg(1, "kept"))
    assert Store(p).segments(m)[0]["text"] == "kept"
