"""Lỗi 21c (option C, rnd review/VIEC_21c_rnd_ket_qua.md): the MoM is written from the TRANSCRIPT after Stop, not from the
live notes (those froze on long meetings: meeting 63 kept ~6 of 109 minutes).
  1. the final sentences are cut into 600 s parts by sentence time;
  2. map: one Gemma pass per part (prompts/mom_map.md) -> a bullet list of the points of that part;
     finish=length => the part is split in two and run again (a cut list loses the end of the part);
  3. "Main content" is put together by CODE: one "### [mm:ss–mm:ss]" per part + its points, so no point is rewritten or dropped;
  4. reduce: one Gemma pass (prompts/mom_reduce.md) for title, summary, decisions, action items, terms;
     finish=length => run once more, then keep it and say so in the warnings (the points are safe in Main content);
  5. Main content is inserted as the body of the 2nd "## " section, whatever Gemma wrote there.
gen(tag, prompt, max_tokens) -> (text, stats) is given by the caller (daemon worker or mom_rebuild's own worker).
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_MAP = open(os.path.join(HERE, "prompts", "mom_map.md")).read()
PROMPT_REDUCE = open(os.path.join(HERE, "prompts", "mom_reduce.md")).read()
PART_S, MAP_TOKENS, REDUCE_TOKENS = 600, 1200, 3000


def mmss(s):
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def chunks(finals, part_s=PART_S):
    """Final sentences (dicts with s, e, text, speaker, dropped_lang) -> lists of sentences, one per part_s window."""
    rows = sorted((f for f in finals if f.get("text") and not f.get("dropped_lang")), key=lambda f: f["s"])
    out = []
    for f in rows:
        if out and int(f["s"] // part_s) == int(out[-1][0]["s"] // part_s):
            out[-1].append(f)
        else:
            out.append([f])
    return out


def map_part(ch, language, gen, info):
    """[(start_s, end_s, [points])] for one part; a cut answer => split in two and run again."""
    a, b = ch[0]["s"], ch[-1]["e"]
    tr = "\n".join(f"[{mmss(f['s'])}] {f.get('speaker') or 'Speaker ?'}: {f['text']}" for f in ch)
    text, st = gen(f"mcmap{info['map_calls']}", PROMPT_MAP.replace("{{LANGUAGE}}", language).replace("{{FROM}}", mmss(a))
                   .replace("{{TO}}", mmss(b)).replace("{{TRANSCRIPT}}", tr), MAP_TOKENS)
    info["map_calls"] += 1; info["gen_s"] += st.get("wall_s", 0); info["gen_tokens"] += st.get("gen_tokens", 0)
    if st.get("finish") == "length":
        info["map_cut"] += 1
        if len(ch) > 1:
            info["map_split"] += 1
            h = len(ch) // 2
            return map_part(ch[:h], language, gen, info) + map_part(ch[h:], language, gen, info)
        info["warn"].append(f"part {mmss(a)} cut on a single sentence: kept as is")
    pts = [l.strip() for l in text.split("\n") if l.strip().startswith(("-", "*")) and "(none)" not in l]
    return [(a, b, pts)]


def parts_text(parts):
    return "\n\n".join(f"### Part [{mmss(a)}–{mmss(b)}]\n" + "\n".join(p) for a, b, p in parts)


def main_from_parts(parts):
    out = []
    for a, b, pts in parts:
        if pts:
            out.append(f"### [{mmss(a)}–{mmss(b)}]")
            out += ["* " + re.sub(r"^[-*]\s*(\((D|T)\)\s*)?", "", p) for p in pts]
    return "\n".join(out)


def insert_main(md, main):
    """`main` becomes the body of the 2nd '## ' section, whatever Gemma wrote there (rnd: it dropped the {{MAIN}} line 1 time in 4)."""
    L = md.split("\n")
    h = [i for i, l in enumerate(L) if l.startswith("## ")]
    if len(h) < 2:
        return md.rstrip() + "\n\n## Main content\n" + main + "\n"
    end = h[2] if len(h) > 2 else len(L)
    return "\n".join(L[:h[1] + 1] + [main, ""] + L[end:])


# Gemma sometimes leaves the template's English headings in a Vietnamese MoM (measured 02/10: 1 of 4 runs) => fixed by code.
VI_HEADINGS = {"summary": "Tóm tắt", "main content": "Nội dung chính", "decisions / points agreed": "Quyết định / điểm đã thống nhất",
               "action items": "Việc cần làm", "terms / systems mentioned": "Thuật ngữ / hệ thống được đề cập"}


def vi_headings(md):
    md = re.sub(r"^## (.+?)\s*$", lambda m: "## " + VI_HEADINGS.get(m.group(1).strip().lower(), m.group(1).strip()), md, flags=re.M)
    return re.sub(r"^([*-] )?None\.?\s*$", lambda m: (m.group(1) or "") + "Không có", md, flags=re.M)


def build_mom(finals, language, gen, part_s=PART_S):
    """-> (mom markdown, parts, info). info: numbers only (parts, map calls / cut / split, reduce cut, Gemma seconds / tokens, warnings)."""
    info = {"parts": 0, "map_calls": 0, "map_cut": 0, "map_split": 0, "reduce_calls": 0, "reduce_cut": 0, "gen_s": 0.0, "gen_tokens": 0, "warn": []}
    parts = []
    for ch in chunks(finals, part_s):
        parts += map_part(ch, language, gen, info)
    info["parts"] = len(parts)
    if not parts:
        return "", parts, info
    p = PROMPT_REDUCE.replace("{{LANGUAGE}}", language).replace("{{PARTS}}", parts_text(parts))
    for _ in range(2):                                   # a cut reduce runs once more, then is kept with a warning
        text, st = gen(f"mcred{info['reduce_calls']}", p, REDUCE_TOKENS)
        info["reduce_calls"] += 1; info["gen_s"] += st.get("wall_s", 0); info["gen_tokens"] += st.get("gen_tokens", 0)
        if st.get("finish") != "length":
            break
        info["reduce_cut"] += 1
    else:
        info["warn"].append("reduce cut twice: summary / decisions / action items may be incomplete (Main content is complete)")
    info["gen_s"] = round(info["gen_s"], 1)
    md = insert_main(text, main_from_parts(parts))
    return (vi_headings(md) if language == "Vietnamese" else md), parts, info


def main_rows(md):
    """The Word form's highlight rows from a MoM's Main content: [(time range, [points])] per '### [..]' heading; [] for an old MoM."""
    secs = re.split(r"^## ", md, flags=re.M)
    rows = []
    for l in (secs[2] if len(secs) > 2 else "").split("\n"):
        m = re.match(r"^###\s*\[(\d+:\d\d\s*[–-]\s*\d+:\d\d)\]", l.strip())
        if m:
            rows.append((m.group(1), []))
        elif rows and re.match(r"^\s*[*\-]\s+\S", l):
            rows[-1][1].append(re.sub(r"^\s*[*\-]\s+", "", l).strip())
    return [r for r in rows if r[1]]


if __name__ == "__main__":
    fin = [{"s": s, "e": s + 5, "text": f"câu {s}", "speaker": "Speaker 1", "dropped_lang": False} for s in range(0, 1500, 30)]
    fin.append({"s": 10, "e": 12, "text": "rác ngôn ngữ khác", "speaker": "", "dropped_lang": True})
    assert [len(c) for c in chunks(fin)] == [20, 20, 10] and all(not f["dropped_lang"] for c in chunks(fin) for f in c)
    calls = []

    def fake(tag, prompt, mt):
        calls.append(tag)
        n = prompt.count("\n[")                              # sentences in a map prompt
        if tag.startswith("mcmap") and n > 12:               # a long part is "cut" => must be split and re-run
            return "- (D) điểm bị cắt", {"finish": "length", "wall_s": 1, "gen_tokens": 1200}
        if tag.startswith("mcmap"):
            return f"- (T) ý {tag}\n- (none)\n* ý hai", {"finish": "stop", "wall_s": 1, "gen_tokens": 50}
        if tag == "mcred0":
            return "# Tiêu đề", {"finish": "length", "wall_s": 2, "gen_tokens": 3000}
        return "# Họp thử\n\n## Tóm tắt\nNgắn.\n\n## Nội dung chính\nGemma tự viết, phải bị thay\n\n## Quyết định\n- Không có", {"finish": "stop", "wall_s": 2, "gen_tokens": 300}
    md, parts, info = build_mom(fin, "Vietnamese", fake)
    assert info["map_cut"] == 2 and info["map_split"] == 2 and info["parts"] == 5, info      # parts 1-2 (20 sentences) cut once each
    assert info["reduce_cut"] == 1 and info["reduce_calls"] == 2 and not info["warn"], info
    assert "Gemma tự viết" not in md and "### [00:00–04:35]" in md and "* ý mcmap1" in md and "(T)" not in md and "(none)" not in md, md
    assert md.index("## Nội dung chính") < md.index("### [00:00") < md.index("## Quyết định") and md.startswith("# Họp thử")
    assert all("bị cắt" not in p for _, _, pts in parts for p in pts), "a cut map answer must never be kept when it can be split"
    assert vi_headings("## Summary\nx\n## Action items\nNone\n- None\n## Tóm tắt riêng") == "## Tóm tắt\nx\n## Việc cần làm\nKhông có\n- Không có\n## Tóm tắt riêng"
    e, _, _ = build_mom(fin[:3], "English", lambda t, p, m: ("# T\n## Summary\nNone\n## Main content\n", {"finish": "stop"}))
    assert "## Summary" in e and "None" in e, "only a Vietnamese MoM gets its headings rewritten"
    rows = main_rows(md)
    assert len(rows) == 5 and rows[0][0] == "00:00–04:35" and rows[0][1] == ["ý mcmap1", "ý hai"], rows
    assert main_rows("# x\n## a\n## b\n- old bullet") == [], "an old MoM (no part headings) gives no rows"
    assert insert_main("# t\n## A\nx", "M").endswith("## Main content\nM\n")
    print("mom_c self-check OK")
