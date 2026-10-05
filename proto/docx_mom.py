"""Việc 13: the Word MoM (templates/mom_template.docx) filled from a meeting's notes by the local Gemma.
  docx_mom.py --meeting-id N --out <file.docx> [--db run/midy.db]      (the daemon command `export_docx` calls export())
The template is the frame: header, styles, numbering and table formatting are kept byte for byte; any other text in it
(e.g. an old meeting, if you swap in your own filled MoM) is replaced or removed, and check_clean() proves it (each paragraph of the output must be a label or a value
written by this fill, otherwise the file is not written). Standard library only (zipfile + the XML text), no python-docx.
"""
import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from glossary import VI, VI_OUTPUT_RATIO, output_language  # noqa: E402
import stamps  # noqa: E402
import mom_c  # noqa: E402

TEMPLATE = os.environ.get("MIDY_MOM_TEMPLATE", os.path.join(HERE, "..", "templates", "mom_template.docx"))
PROMPT = open(os.path.join(HERE, "prompts", "mom_docx.md")).read()
# Labels of the template, translated to the meeting language (29/09: "dịch theo ngôn ngữ họp").
# ponytail: Vietnamese only; other meeting languages keep the template's English labels (values are still in that language).
VI_LABELS = {
    "Minutes of Meeting": "Biên bản cuộc họp", "Meeting Details": "Thông tin cuộc họp", "Meeting Title:": "Tên cuộc họp:",
    "Meeting Date:": "Thời gian:", "Location:": "Địa điểm:", "Organizer:": "Người chủ trì:", "Invitees:": "Thành phần mời:",
    "Prepared By:": "Người lập:", "Date Prepared:": "Ngày lập:", "Objective:": "Mục tiêu:", "No.": "STT",
    "Meeting Highlight/ Notes": "Nội dung chính / Ghi chú", "Action To Be Taken": "Việc cần làm", "Target": "Thời hạn",
    "Summary of Action Items:": "Tổng hợp việc cần làm:", "Action": "Việc cần làm", "Owner": "Người phụ trách",
    "Date/ Phase": "Thời hạn / Giai đoạn", "N/A": "Không có",
}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")

P_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)
T_RE = re.compile(r"<w:t(?: [^>]*)?>([^<]*)</w:t>")
TC_RE = re.compile(r"<w:tc>.*?</w:tc>", re.S)
TR_RE = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)


def ptext(xml):
    return "".join(T_RE.findall(xml)).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def set_text(p, text):
    """Paragraph p with `text`: the first w:t takes it, the other w:t are emptied (runs, drawings and formatting stay)."""
    ts = list(T_RE.finditer(p))
    if not ts:                                   # empty paragraph: add one run with the paragraph-mark formatting
        m = re.search(r"<w:pPr>.*?(<w:rPr>.*?</w:rPr>).*?</w:pPr>", p, re.S)
        run = f'<w:r>{m.group(1) if m else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>' if text else ""
        return p[:-len("</w:p>")] + run + "</w:p>" if not p.endswith("/>") else p
    out, last = [], 0
    for i, m in enumerate(ts):
        out.append(p[last:m.start()])
        out.append(f'<w:t xml:space="preserve">{escape(text)}</w:t>' if i == 0 else "<w:t></w:t>")
        last = m.end()
    return "".join(out) + p[last:]


def set_cell(tc, lines):
    """Cell with one paragraph per line, each a copy of the cell's first paragraph (same pPr/rPr)."""
    ps = list(P_RE.finditer(tc))
    proto = ps[0].group(0)
    new = "".join(set_text(proto, line) for line in (lines or [""]))
    return tc[:ps[0].start()] + new + tc[ps[-1].end():]


def strip_ids(xml):                              # cloned rows/paragraphs must not repeat Word's w14 paragraph ids
    return re.sub(r' w14:(?:paraId|textId)="[0-9A-F]+"', "", xml)


def fill_row(tr, values):
    cells = list(TC_RE.finditer(tr))
    assert len(cells) == len(values), (len(cells), len(values))
    out, last = [], 0
    for m, v in zip(cells, values):
        out += [tr[last:m.start()], set_cell(m.group(0), v if isinstance(v, list) else str(v).split("\n"))]
        last = m.end()
    return strip_ids("".join(out) + tr[last:])


def fill_table(tbl, L, rows):
    trs = list(TR_RE.finditer(tbl))
    head = trs[0].group(0)
    head = fill_row(head, [L(ptext(m.group(0)).strip()) for m in TC_RE.finditer(head)])
    body = "".join(fill_row(trs[1].group(0), r) for r in rows)          # row 1 = the formatting prototype of a data row
    return tbl[:trs[0].start()] + head + body + tbl[trs[-1].end():]


def fill_details(tbl, L, vals):
    """Meeting Details table: label cells are translated, the cell after a label takes that label's value."""
    def row(m):
        tr, out, last, key = m.group(0), [], 0, None
        for c in TC_RE.finditer(tr):
            t = ptext(c.group(0)).strip()
            if key is None and (t.endswith(":") or t == "Meeting Details"):
                new, key = set_cell(c.group(0), [L(t)]), (t if t.endswith(":") else None)
            else:
                new, key = set_cell(c.group(0), [vals.get(key, "")]), None
            out += [tr[last:c.start()], new]; last = c.end()
        return "".join(out) + tr[last:]
    return TR_RE.sub(row, tbl)


def fill_document(x, L, d):
    """document.xml of the template -> the new MoM. d = the fill values (see export())."""
    start, end = x.index("<w:body>") + len("<w:body>"), x.index("<w:sectPr")
    blocks = re.finditer(r"<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>|<w:p/>", x[start:end], re.S)
    out, bullet = [], None
    for b in blocks:
        s = b.group(0)
        if s.startswith("<w:tbl>"):
            head = ptext(TR_RE.search(s).group(0))
            if head.strip().startswith("Meeting Details"):
                s = fill_details(s, L, d["details"])
            elif "Highlight" in head:
                s = fill_table(s, L, d["highlights"])
            else:
                s = fill_table(s, L, d["actions"])
            out.append(s)
            continue
        t = ptext(s).strip()
        if 'w:val="ListBullet"' in s:                       # objective bullets: keep the first as the prototype, drop all
            if bullet is None:
                bullet = s
                out.append("\0BULLETS\0")
            continue
        if t in VI_LABELS:
            out.append(set_text(s, L(t)))
        elif t:                                              # any other old text (e.g. a footnote line) goes
            continue
        else:
            out.append(s)
    body = "".join(out).replace("\0BULLETS\0", "".join(strip_ids(set_text(bullet, o)) for o in d["objective"]))
    return x[:start] + body + x[end:]


def check_clean(parts, allowed):
    """Every non-empty paragraph of the output must be a label or a value of this fill: no text of the old meeting is left."""
    bad = [ptext(p).strip() for xml in parts for p in P_RE.findall(xml) if ptext(p).strip() and ptext(p).strip() not in allowed]
    return bad


def write_docx(out, L, d, now):
    with zipfile.ZipFile(TEMPLATE) as z:          # closed after each export (the daemon is long-lived: was one open fd per export)
        _write_docx(z, out, L, d, now)


def _write_docx(z, out, L, d, now):
    doc = fill_document(z.read("word/document.xml").decode(), L, d)
    hdr = z.read("word/header1.xml").decode()
    hdr = P_RE.sub(lambda m: set_text(m.group(0), L(ptext(m.group(0)).strip())) if ptext(m.group(0)).strip() in VI_LABELS else m.group(0), hdr)
    allowed = {L(k) for k in VI_LABELS} | set(d["allowed"])
    bad = check_clean([doc, hdr], allowed)
    if bad:
        raise ValueError(f"template text left in the output: {len(bad)} paragraph(s)")
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = z.read("docProps/core.xml").decode()
    core = re.sub(r"(<dc:creator>)[^<]*(</dc:creator>)", r"\1Middy\2", core)
    core = re.sub(r"(<cp:lastModifiedBy>)[^<]*(</cp:lastModifiedBy>)", r"\1Middy\2", core)
    core = re.sub(r"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)", rf"\g<1>{stamp}\2", core)
    core = re.sub(r"(<cp:revision>)[^<]*(</cp:revision>)", r"\g<1>1\2", core)
    app = re.sub(r"(<Company>)[^<]*(</Company>)", r"\1\2", z.read("docProps/app.xml").decode())
    new = {"word/document.xml": doc, "word/header1.xml": hdr, "docProps/core.xml": core, "docProps/app.xml": app}
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():                    # same parts, same order; only the four above change
            w.writestr(info, new[info.filename].encode() if info.filename in new else z.read(info.filename))
    os.chmod(tmp, 0o600); os.replace(tmp, out)


# ---- LLM ----------------------------------------------------------------------------------------------------------------
def ask_gemma(prompt, run_root):
    """One generation on a fresh local Gemma worker (same worker and offline env as the meeting pipeline)."""
    from core import ENV, PY
    sock = os.path.join(run_root, f"docx_{os.getpid()}.sock")
    if os.path.exists(sock):
        os.unlink(sock)
    w = subprocess.Popen([PY, os.path.join(HERE, "llm_worker.py"), "--socket", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        for _ in range(900):
            if os.path.exists(sock) or w.poll() is not None:   # a worker that died while loading: fail now, not after 90 s
                break
            time.sleep(0.1)
        c.connect(sock); r, f = c.makefile("r", encoding="utf-8"), c.makefile("w", encoding="utf-8")
        json.loads(r.readline())
        f.write(json.dumps({"cmd": "generate", "id": "docx", "messages": [{"role": "user", "content": prompt}], "max_tokens": 3000}) + "\n"); f.flush()
        while True:
            e = json.loads(r.readline())
            if e.get("event") == "done":
                f.write(json.dumps({"cmd": "quit"}) + "\n"); f.flush()
                return e
    finally:
        c.close()
        try:
            w.wait(timeout=20)
        except subprocess.TimeoutExpired:
            w.kill()
        if os.path.exists(sock):
            os.unlink(sock)


def parse_reply(text):
    """Gemma's JSON -> the fill dict with the expected types; None when it is not JSON."""
    try:
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    s = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()
    items = lambda k: [x for x in (j.get(k) or []) if isinstance(x, dict)]
    return {"title": s(j.get("title")), "location": s(j.get("location")), "organizer": s(j.get("organizer")), "invitees": s(j.get("invitees")),
            "objective": [s(o) for o in (j.get("objective") or []) if s(o)],
            "highlights": [{k: s(h.get(k)) for k in ("notes", "action", "target")} for h in items("highlights") if s(h.get("notes"))],
            "actions": [{k: s(a.get(k)) for k in ("action", "owner", "date")} for a in items("actions") if s(a.get("action"))]}


def vi_ratio(j):
    t = json.dumps(j, ensure_ascii=False)
    letters = sum(c.isalpha() for c in t)
    return len(VI.findall(t)) / letters if letters else 0.0


def meeting_when(m, language):
    t0 = datetime.datetime.fromtimestamp(m["started_at"])
    t1 = t0 + datetime.timedelta(seconds=m.get("audio_end_s") or 0)
    hm = f"{t0:%H:%M}–{t1:%H:%M}" if f"{t1:%H:%M}" != f"{t0:%H:%M}" else f"{t0:%H:%M}"
    return f"{t0:%d/%m/%Y}, {hm}" if language == "Vietnamese" else f"{t0.day} {MONTHS[t0.month - 1]} {t0.year}, {hm}"


def saved_form(store, mid):
    """The form Claude Code saved with its minutes (daemon `save_minutes`, kept in the MoM note's stats), or None."""
    return next((n["stats"].get("form") for n in store.notes(mid, "mom")), None)


def export(store, mid, out, run_root=os.path.join(HERE, "..", "run"), now=None, form=None):
    """Write the Word MoM of meeting `mid` to `out`. Returns numbers only (no meeting text).
    form: the fill (title, objective, highlights, actions, ...) written by Claude Code; given or saved => no Gemma at all."""
    now = now or datetime.datetime.now().astimezone()
    m = store.meeting(mid)
    if not m:
        return {"ok": False, "error": "unknown meeting"}
    notes = {n["kind"]: n["text"] for n in sorted(store.notes(mid), key=lambda n: n["idx"])}   # last idx of each kind wins
    src = notes.get("user") or notes.get("mom") or notes.get("live") or ""
    form = form or saved_form(store, mid)
    if not src.strip() and not form:
        return {"ok": False, "error": "this meeting has no notes"}
    segs = store.segments(mid)
    language = output_language([s["text"] for s in segs if s.get("text")], m["language"] or "English")
    L = (lambda t: VI_LABELS.get(t, t)) if language == "Vietnamese" else (lambda t: t)
    # Lỗi 21c: a MoM written from the transcript has one "### [mm:ss–mm:ss]" part per 10 minutes in its Main content => the
    # highlight rows are built by CODE (one per part, every point kept, no cap of 12); Gemma only fills title / objective /
    # actions from the rest of the MoM. An older MoM (no part headings) keeps the Gemma-written highlights.
    rows = mom_c.main_rows(src)
    warn, tries = [], 0
    if form:
        j, e = parse_reply(json.dumps(form, ensure_ascii=False)), {"stats": {}}
        if j and j["highlights"]:                    # Claude's own highlight rows win over the per-part rows
            rows = []
    else:
        prompt = PROMPT if not rows else "\n".join(l for l in PROMPT.split("\n") if '"highlights"' not in l)
        notes_in = stamps.clean(mom_c.insert_main(src, "") if rows else src)
        prompt = prompt.replace("{{LANGUAGE}}", language).replace("{{NOTES}}", notes_in)     # Lỗi 21: Gemma writes no times
        for tries in (1, 2):                         # rnd rule 6.3: one retry when the reply is not JSON or not in Vietnamese
            e = ask_gemma(prompt, run_root)
            j = parse_reply(e["text"])
            if j and (language != "Vietnamese" or vi_ratio(j) >= VI_OUTPUT_RATIO):
                break
    if not j:
        return {"ok": False, "error": "the local model did not return the form", "tries": tries}
    if language == "Vietnamese" and vi_ratio(j) < VI_OUTPUT_RATIO:
        warn.append("language")
    if e["stats"].get("finish") == "length":
        warn.append("truncated")
    na = L("N/A")
    ix = stamps.Index([(s["s"], s["text"]) for s in segs if s.get("text") and not s.get("dropped_lang")])
    for h in j["highlights"]:                         # Lỗi 21: the time of a topic = the transcript sentence that backs it, or none
        h["notes"] = stamps.clean(h["notes"]).strip()
        t = ix.find(h["notes"]) if ix.rows else None
        if t is not None:
            h["notes"] = f"[{stamps.mmss(t)}] {h['notes']}"
    for a in j["actions"]:
        a["action"] = stamps.clean(a["action"]).strip()
    if rows:
        hl = [[str(i), [f"[{rng}]"] + pts, na, ""] for i, (rng, pts) in enumerate(rows, 1)]
    else:
        hl = [[str(i), h["notes"], h["action"] or na, h["target"]] for i, h in enumerate(j["highlights"], 1)] or [["1", na, na, ""]]
    ac = [[str(i), a["action"], a["owner"], a["date"]] for i, a in enumerate(j["actions"], 1)] or [["1", na, "", ""]]
    title = j["title"] or m["name"]
    details = {"Meeting Title:": title, "Meeting Date:": meeting_when(m, language), "Location:": j["location"], "Organizer:": j["organizer"],
               "Invitees:": j["invitees"], "Prepared By:": "", "Date Prepared:": meeting_when({"started_at": now.timestamp()}, language).split(",")[0]}
    objective = j["objective"] or [na]
    allowed = set(details.values()) | set(objective) | {x for r in hl + ac for v in r for x in (v if isinstance(v, list) else [v])}
    write_docx(out, L, {"details": details, "objective": objective, "highlights": hl, "actions": ac, "allowed": allowed}, now)
    return {"ok": True, "path": out, "language": language, "highlights": len(hl), "highlights_from_parts": bool(rows), "highlight_points": sum(len(p) for _, p in rows), "actions": len(j["actions"]), "objective": len(j["objective"]),
            "tries": tries, "warn": warn, "gen_tokens": e["stats"].get("gen_tokens"), "wall_s": e["stats"].get("wall_s")}


def _selftest():
    """Fill the template with fixed values (no LLM): labels translated, old text gone, formatting parts unchanged."""
    import tempfile
    vals = {"details": {"Meeting Title:": "Kiểm thử", "Meeting Date:": "29/09/2026, 10:00–10:30", "Date Prepared:": "29/09/2026"},
            "objective": ["Mục tiêu A", "Mục tiêu B"], "highlights": [["1", "Chủ đề. Nội dung", "Không có", ""], ["2", "Chủ đề 2 & <x>", "Làm X", "Thứ Hai"]],
            "actions": [["1", "Làm X", "Anh A", "Thứ Hai"]]}
    vals["allowed"] = set(vals["details"].values()) | set(vals["objective"]) | {v for r in vals["highlights"] + vals["actions"] for v in r}
    out = os.path.join(tempfile.mkdtemp(), "t.docx")
    write_docx(out, lambda t: VI_LABELS.get(t, t), vals, datetime.datetime.now().astimezone())
    z, z0 = zipfile.ZipFile(out), zipfile.ZipFile(TEMPLATE)
    doc = z.read("word/document.xml").decode()
    texts = [ptext(p).strip() for p in P_RE.findall(doc) if ptext(p).strip()]
    assert "Biên bản cuộc họp" in ptext(z.read("word/header1.xml").decode())
    assert texts.count("Mục tiêu A") == 1 and "Chủ đề 2 & <x>" in texts and "Thông tin cuộc họp" in texts and "Tổng hợp việc cần làm:" in texts
    assert doc.count("<w:tr ") + doc.count("<w:tr>") == 6 + 1 + 2 + 1 + 1, "details 6 rows + 2 table headers + 2 highlight rows + 1 action row"
    assert not check_clean([doc], {VI_LABELS[k] for k in VI_LABELS} | vals["allowed"])
    assert check_clean([doc], {VI_LABELS[k] for k in VI_LABELS}) and check_clean([z0.read("word/document.xml").decode()], vals["allowed"]), "the guard must flag text it was not given"
    assert meeting_when({"started_at": 0, "audio_end_s": 20}, "English").count(":") == 1
    assert [i.filename for i in z.infolist()] == [i.filename for i in z0.infolist()]
    for part in ("word/styles.xml", "word/numbering.xml", "word/_rels/document.xml.rels"):
        assert z.read(part) == z0.read(part), part
    assert "<dc:creator>Middy</dc:creator>" in z.read("docProps/core.xml").decode()
    print("docx_mom self-check OK", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting-id", type=int)
    ap.add_argument("--out")
    ap.add_argument("--db", default=os.path.join(HERE, "..", "run", "midy.db"))
    ap.add_argument("--selftest", action="store_true")
    A = ap.parse_args()
    if A.selftest:
        _selftest(); sys.exit()
    from store import Store
    print(json.dumps(export(Store(A.db), A.meeting_id, A.out)))
