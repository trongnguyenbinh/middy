"""SQLite + FTS5 store (design ⑥). One file, WAL mode, every final segment committed as it arrives so a crash
loses nothing that was already final. Also holds the term glossary (design ⑫) per space ('*' = every space).
"""
import json
import os
import sqlite3
import time

SCHEMA = """
create table if not exists meetings(id integer primary key, name text, space text, language text, source text,
    started_at real, status text, audio_end_s real default 0, num_speakers integer default -1);
create table if not exists segments(id integer primary key, meeting_id integer, seq integer, s real, e real, speaker text,
    text text, text_raw text, dropped_lang integer, ctx text, ctx_leak integer, corrections integer, stream text, created_at real,
    unique(meeting_id, seq));
create virtual table if not exists segments_fts using fts5(text, content='segments', content_rowid='id');
create trigger if not exists segments_ai after insert on segments begin
    insert into segments_fts(rowid, text) values (new.id, new.text); end;
create trigger if not exists segments_au after update of text on segments begin
    insert into segments_fts(segments_fts, rowid, text) values ('delete', old.id, old.text);
    insert into segments_fts(rowid, text) values (new.id, new.text); end;
create trigger if not exists segments_ad after delete on segments begin
    insert into segments_fts(segments_fts, rowid, text) values ('delete', old.id, old.text); end;
create table if not exists slides(id integer primary key, meeting_id integer, t real, words text, ocr_text text, created_at real);
create index if not exists slides_meeting on slides(meeting_id);
create table if not exists notes(id integer primary key, meeting_id integer, kind text, idx integer, text text, stats text, created_at real,
    unique(meeting_id, kind, idx));
create table if not exists glossary(id integer primary key, space text, kind text, wrong text, right text, created_at real,
    unique(space, kind, wrong));
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)   # autocommit
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")
        self.db.executescript(SCHEMA)

    # ---- meetings
    def new_meeting(self, name, space, language, source, num_speakers=-1):
        cur = self.db.execute("insert into meetings(name, space, language, source, started_at, status, num_speakers) values (?,?,?,?,?,?,?)",
                              (name, space, language, json.dumps(source), time.time(), "recording", num_speakers))
        return cur.lastrowid

    def meeting(self, mid):
        r = self.db.execute("select id, name, space, language, source, started_at, status, audio_end_s, num_speakers from meetings where id=?", (mid,)).fetchone()
        return r and dict(zip(("id", "name", "space", "language", "source", "started_at", "status", "audio_end_s", "num_speakers"), r))

    def meetings(self):
        return [dict(zip(("id", "name", "status", "started_at", "audio_end_s", "space", "language"), r)) for r in
                self.db.execute("select id, name, status, started_at, audio_end_s, space, language from meetings order by id desc")]

    def set_name(self, mid, name):
        self.db.execute("update meetings set name=? where id=?", (name, mid))

    def set_language(self, mid, language):                       # Lỗi 10: latest recognition language (resume + library)
        self.db.execute("update meetings set language=? where id=?", (language, mid))

    def set_space(self, mid, space):
        self.db.execute("update meetings set space=? where id=?", (space, mid))

    def set_run_dir(self, mid, run_dir):
        m = self.meeting(mid); src = json.loads(m["source"]) if m else {}
        src["run_dir"] = run_dir
        self.db.execute("update meetings set source=? where id=?", (json.dumps(src), mid))

    def delete_meeting(self, mid):
        """Remove a meeting and everything it owns: notes, slides, segments (+ FTS rows via trigger), the meetings row.
        Returns the run directory recorded for it (or None) so the caller can remove the files too."""
        m = self.meeting(mid)
        if not m:
            return None
        run_dir = json.loads(m["source"]).get("run_dir")
        for t in ("notes", "slides", "segments"):
            self.db.execute(f"delete from {t} where meeting_id=?", (mid,))
        self.db.execute("delete from meetings where id=?", (mid,))
        return run_dir

    def delete_meetings(self, mids):
        """Việc 18: several meetings in ONE transaction (all rows or none). Returns {id: run_dir recorded or None}."""
        dirs = {}
        self.db.execute("begin")
        try:
            for mid in mids:
                dirs[mid] = self.delete_meeting(mid)
            self.db.execute("commit")
        except Exception:
            self.db.execute("rollback")
            raise
        return dirs

    def counts(self, mid):
        return {t: self.db.execute(f"select count(*) from {t} where meeting_id=?", (mid,)).fetchone()[0] for t in ("segments", "notes", "slides")} | \
               {"fts": self.db.execute("select count(*) from segments_fts where rowid in (select id from segments where meeting_id=?)", (mid,)).fetchone()[0],
                "meetings": self.db.execute("select count(*) from meetings where id=?", (mid,)).fetchone()[0]}

    def spaces(self):
        return [r[0] for r in self.db.execute("select distinct space from meetings order by space")]

    def set_status(self, mid, status):
        self.db.execute("update meetings set status=? where id=?", (status, mid))

    # ---- segments (final groups)
    def add_segment(self, mid, f):
        self.db.execute("insert or ignore into segments(meeting_id, seq, s, e, speaker, text, text_raw, dropped_lang, ctx, ctx_leak, corrections, stream, created_at)"
                        " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, f["id"], f["s"], f["e"], f.get("speaker", ""), f["text"], f.get("text_raw", ""), int(f["dropped_lang"]), f.get("ctx", ""),
                         int(f.get("ctx_leak", False)), f.get("n_corrections", 0), f.get("stream", "spk"), time.time()))
        self.db.execute("update meetings set audio_end_s=max(audio_end_s, ?) where id=?", (f["e"], mid))

    def set_speaker(self, mid, seq, speaker):
        self.db.execute("update segments set speaker=? where meeting_id=? and seq=?", (speaker, mid, seq))

    def segments(self, mid, since_seq=0):
        cols = ("id", "seq", "s", "e", "speaker", "text", "text_raw", "dropped_lang", "ctx", "ctx_leak", "n_corrections", "stream")
        return [dict(zip(cols, r)) for r in self.db.execute(
            "select seq, seq, s, e, speaker, text, text_raw, dropped_lang, ctx, ctx_leak, corrections, stream from segments where meeting_id=? and seq>? order by seq",
            (mid, since_seq))]

    def search(self, q, limit=50):
        sql = ("select s.meeting_id, s.seq, s.s, s.speaker, snippet(segments_fts, 0, '[', ']', '…', 12) from segments_fts join segments s on s.id = segments_fts.rowid"
               " where segments_fts match ? order by rank limit ?")
        try:
            rows = self.db.execute(sql, (q, limit)).fetchall()
        except sqlite3.OperationalError:          # typed text is not valid FTS5 syntax ("KPI-Q4", a lone quote): search the words as phrases
            rows = self.db.execute(sql, (" ".join('"' + w.replace('"', '""') + '"' for w in q.split()) or '""', limit)).fetchall()
        return [dict(zip(("meeting_id", "seq", "s", "speaker", "snippet"), r)) for r in rows]

    # ---- slides / notes
    def add_slide(self, mid, t, words, ocr_text):
        self.db.execute("insert into slides(meeting_id, t, words, ocr_text, created_at) values (?,?,?,?,?)", (mid, t, json.dumps(words), ocr_text, time.time()))

    def slides(self, mid):
        return [{"t": t, "words": json.loads(w), "ocr_text": o} for t, w, o in self.db.execute("select t, words, ocr_text from slides where meeting_id=? order by t", (mid,))]

    def set_note(self, mid, kind, idx, text, stats=None):
        self.db.execute("insert or replace into notes(meeting_id, kind, idx, text, stats, created_at) values (?,?,?,?,?,?)",
                        (mid, kind, idx, text, json.dumps(stats or {}), time.time()))

    def notes(self, mid, kind=None):
        q, a = "select kind, idx, text, stats from notes where meeting_id=?", [mid]
        if kind:
            q += " and kind=?"; a.append(kind)
        return [{"kind": k, "idx": i, "text": t, "stats": json.loads(s)} for k, i, t, s in self.db.execute(q + " order by kind, idx", a)]

    # ---- glossary
    def glossary_add(self, space, kind, wrong, right=""):
        self.db.execute("insert or replace into glossary(space, kind, wrong, right, created_at) values (?,?,?,?,?)", (space, kind, wrong, right, time.time()))

    def glossary(self, space):
        """Merged view for one space (+ '*'), in the JSON shape glossary.py reads."""
        g = {"corrections": {}, "terms": [], "ambiguous": []}
        for k, w, r in self.db.execute("select kind, wrong, right from glossary where space in (?, '*') order by space", (space,)):
            if k == "correction":
                g["corrections"][w] = r
            elif k == "term":
                g["terms"].append(w)
            elif k == "ambiguous":
                g["ambiguous"].append(w)
        return g

    def import_glossary_json(self, space, path):
        d = json.load(open(path))
        for w, r in d.get("corrections", {}).items():
            self.glossary_add(space, "correction", w, r)
        for t in d.get("terms", []):
            self.glossary_add(space, "term", t)
        for a in d.get("ambiguous", []):
            self.glossary_add(space, "ambiguous", a)


if __name__ == "__main__":  # self-check
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "t.db")
    st = Store(p)
    m = st.new_meeting("t", "default", "English", {"file": "x"})
    st.add_segment(m, {"id": 1, "s": 0.0, "e": 5.0, "text": "goods receipt GRN done", "dropped_lang": False})
    st.add_segment(m, {"id": 1, "s": 0.0, "e": 5.0, "text": "dup", "dropped_lang": False})     # same seq -> ignored (crash replay safe)
    st.add_segment(m, {"id": 2, "s": 5.0, "e": 9.0, "text": "next topic", "dropped_lang": False})
    assert [x["text"] for x in st.segments(m)] == ["goods receipt GRN done", "next topic"]
    assert st.meeting(m)["audio_end_s"] == 9.0
    assert st.search("grn")[0]["seq"] == 1 and st.search("nothing") == []
    st.set_speaker(m, 1, "Speaker 2"); assert st.segments(m)[0]["speaker"] == "Speaker 2"
    st.glossary_add("*", "correction", "purchasing api", "Purchasing A/P"); st.glossary_add("ops", "term", "GRN"); st.glossary_add("ops", "ambiguous", "cả ba")
    assert st.glossary("ops") == {"corrections": {"purchasing api": "Purchasing A/P"}, "terms": ["GRN"], "ambiguous": ["cả ba"]}
    assert st.glossary("other") == {"corrections": {"purchasing api": "Purchasing A/P"}, "terms": [], "ambiguous": []}
    st.set_note(m, "part", 0, "notes"); st.set_note(m, "part", 0, "notes v2"); assert st.notes(m, "part")[0]["text"] == "notes v2"
    st2 = Store(p); assert len(st2.segments(m)) == 2, "reopen keeps rows"
    print("store self-check OK")
