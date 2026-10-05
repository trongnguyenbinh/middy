"""Lỗi 21: the [mm:ss] time stamps of the notes, the MoM and the Word form come from the TRANSCRIPT, never from Gemma.
Gemma only ever saw the live notes (no times) when writing the MoM, so "one sub-heading per topic with a time stamp" made it
write "(no time stamp in the notes)" on every line. Now Gemma writes no times; code stamps each bullet / sub-heading with the
start of the transcript sentence that shares the most rare words with it, and leaves the line unstamped when none does.
"""
import math
import re

WORD = re.compile(r"[^\W_]+", re.U)
TS = r"\d{1,3}:\d{2}(?::\d{2})?"
MODEL_TS = re.compile(rf"\s*[\[(]\s*{TS}(?:\s*[-–]\s*{TS})?\s*[\])]")                 # [12:30], (12:30 - 14:00) written by Gemma
# "(Không có dấu thời gian cụ thể trong ghi chú)", "(no time stamp)" ...: a parenthesis that talks about time stamps
PLACEHOLDER = re.compile(r"\s*[\[(][^()\[\]\n]*(dấu thời gian|mốc thời gian|thời điểm cụ thể|time ?stamps?|timestamps?)[^()\[\]\n]*[\])]", re.I)
ITEM = re.compile(r"^(\s*(?:[*\-+]|\d+[.)])\s+|#{3,6}\s+)(.*)$")                       # a bullet or a sub-heading (### …)
STOP = set("""the a an and or of to in on for is are was were be been it this that what which who when where how why do does did
we you they he she i not no with from at by as about will can should would there their its our has have had
và của là có không được cho các những một này đó thì mà với trong ra vào lại đã đang sẽ rồi cũng như để khi nào gì ai sao
bao nhiêu thế vậy anh chị em bạn mình họ ở đây kia thôi nhé ạ à ừ ờ vâng dạ nó cái con người việc làm theo từ về hay hoặc nhưng nên
nếu thể cần phải rất nhiều đến bằng trên dưới sau trước""".split())
MIN_SHARE, MIN_HITS = 0.3, 2         # ponytail: word overlap, not embeddings; a paraphrase with no shared rare word stays unstamped


def mmss(s):
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def feats(text):
    """Content words and pairs of adjacent words (Vietnamese syllables only mean something in pairs: "nhập kho")."""
    w = [x for x in WORD.findall(text.lower())]
    uni = {x for x in w if x not in STOP and (len(x) > 1 or x.isdigit())}
    return uni | {f"{a} {b}" for a, b in zip(w, w[1:]) if not (a in STOP and b in STOP)}


def clean(md):
    """Remove every time stamp and "no time stamp" remark the model wrote; code adds the real ones."""
    return "\n".join(re.sub(r"\s+$", "", MODEL_TS.sub("", PLACEHOLDER.sub("", l))) for l in md.split("\n"))


class Index:
    """rows: [(start_s, text)] of the transcript."""
    def __init__(self, rows):
        self.rows = [(s, feats(t)) for s, t in sorted(rows) if t and t.strip()]
        df = {}
        for _, f in self.rows:
            for x in f:
                df[x] = df.get(x, 0) + 1
        n = len(self.rows)
        self.idf = {x: math.log((n + 1) / d) for x, d in df.items()}

    def find(self, text):
        """Start (s) of the sentence that carries the most of `text`'s rare words, or None. Ties -> the earliest."""
        f = feats(text)
        q = {x for x in f if x in self.idf}
        top = math.log(len(self.rows) + 1)                  # a word the transcript never has counts as the rarest
        total = sum(self.idf.get(x, top) for x in f if x in self.idf or " " not in x)
        if not q:
            return None
        best, best_s = 0.0, None
        for s, f in self.rows:
            hit = q & f
            if sum(1 for x in hit if " " not in x) < MIN_HITS:
                continue
            sc = sum(self.idf[x] for x in hit)
            if sc > best:
                best, best_s = sc, s
        return best_s if best_s is not None and best / total >= MIN_SHARE else None


def stamp(md, rows, sections=None):
    """Clean `md`, then put "[mm:ss] " in front of every bullet / sub-heading that the transcript backs. A sub-heading or a bullet
    with sub-bullets takes the earliest stamp under it. sections: the 1-based "## " sections to stamp (None = all; the MoM stamps
    only its main content and decisions). Returns (text, stamped, lines_considered)."""
    md = clean(md)
    lines = md.split("\n")
    sec, where = 0, []
    for l in lines:
        sec += l.startswith("## ")
        m = ITEM.match(l)
        where.append(m if m and m.group(2).strip() and (sections is None or sec in sections) else None)
    if not rows:
        return md, 0, sum(1 for m in where if m)
    ix = Index(rows)
    found = [ix.find(m.group(2)) if m else None for m in where]
    indent = lambda l: len(l) - len(l.lstrip())
    for i in range(len(lines) - 1, -1, -1):             # bottom-up: a parent takes the earliest of everything under it
        if not where[i]:
            continue
        head = lines[i].lstrip().startswith("#")
        sub = []
        for j in range(i + 1, len(lines)):
            if lines[j].lstrip().startswith("#") or (not head and lines[j].strip() and indent(lines[j]) <= indent(lines[i])):
                break
            if found[j] is not None:
                sub.append(found[j])
        if sub:
            found[i] = min(sub)
    out, n = [], 0
    for l, m, s in zip(lines, where, found):
        if m and s is not None:
            out.append(f"{m.group(1)}[{mmss(s)}] {m.group(2)}"); n += 1
        else:
            out.append(l)
    return "\n".join(out), n, sum(1 for m in where if m)


def keep_known(text, starts, tol=1.0):
    """Ask: drop a [mm:ss] that is not the start of a transcript sentence (the model may misquote one)."""
    def ok(m):
        p = [int(x) for x in m.group(1).split(":")]
        v = p[0] * 60 + p[1] if len(p) == 2 else p[0] * 3600 + p[1] * 60 + p[2]
        return m.group(0) if any(abs(v - s) <= tol for s in starts) else ""
    return PLACEHOLDER.sub("", re.sub(rf"\s*\[({TS})\]", ok, text))


if __name__ == "__main__":
    rows = [(5.0, "xin chào mọi người hôm nay mình bàn về nhập kho"), (65.0, "đơn mua hàng PO được tạo bằng ME21N cho nhà cung cấp"),
            (130.0, "khi nhập kho bằng MIGO movement type 101 sẽ tạo chứng từ vật tư"), (200.0, "ok cảm ơn"), (260.0, "hạn chót thứ sáu chị Lan gửi file")]
    md = ("# Họp nhập kho\n## Nội dung chính\n### Tạo PO (Không có dấu thời gian cụ thể trong ghi chú)\n- Tạo PO bằng ME21N cho nhà cung cấp [99:99]\n"
          "### Nhập kho\n- Nhập kho bằng MIGO movement type 101 tạo chứng từ vật tư\n- Một ý không có trong transcript\n## Hành động\n- Chị Lan gửi file hạn chót thứ sáu\n"
          "- **Các bước:**\n    - Tạo PO bằng ME21N cho nhà cung cấp\n    - Nhập kho bằng MIGO movement type 101\n"
          "| Ai | Việc | Khi nào |\n|---|---|---|\n| Lan | gửi file | thứ sáu |")
    out, n, k = stamp(md, rows)
    L = out.split("\n")
    assert "dấu thời gian" not in out and "99:99" not in out, out
    assert L[2] == "### [01:05] Tạo PO" and L[3] == "- [01:05] Tạo PO bằng ME21N cho nhà cung cấp", L[2:4]
    assert L[4] == "### [02:10] Nhập kho" and L[5].startswith("- [02:10] "), L[4:6]
    assert L[6] == "- Một ý không có trong transcript", "no backing sentence => no stamp, never a placeholder"
    assert L[8].startswith("- [04:20] "), L[8]
    assert L[9] == "- [01:05] **Các bước:**", "a parent bullet takes the earliest of its sub-bullets"
    assert L[-1] == "| Lan | gửi file | thứ sáu |" and L[0] == "# Họp nhập kho", "titles and tables are left alone"
    assert (n, k) == (8, 9), (n, k)
    assert stamp(md, rows, sections={1})[1] == 4, "only the first section"
    assert stamp("- a", []) == ("- a", 0, 1)
    assert keep_known("Ở [02:10] và [07:77] (không có mốc thời gian)", [130.0]) == "Ở [02:10] và", keep_known("Ở [02:10] và [07:77] (không có mốc thời gian)", [130.0])
    assert clean("- Kế hoạch (10:00 - 12:30) xong") == "- Kế hoạch xong"
    print("stamps self-check OK")
