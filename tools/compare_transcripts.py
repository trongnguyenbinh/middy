"""Compare two transcripts of the same meeting (e.g. Midy vs another app). Prints NUMBERS only, never text.

  compare_transcripts.py <midy.md> <other.md> [--chunk-min 5] [--bad-wer 0.3] [--glossary <json>]

Accepted line shapes (either side): "**[hh:mm:ss] Speaker 1:** text", "[mm:ss] Name: text", "hh:mm:ss Name text",
"Name: text", or plain paragraphs. Timestamps and speaker prefixes are optional and detected per line.
Normalisation before comparing: NFC, lower case, punctuation removed, whitespace collapsed (digits kept).
WER is word-level Levenshtein (substitutions + deletions + insertions) / reference length, computed both ways
(A as reference for B, B as reference for A). Per-chunk WER uses timestamps when both sides have them, otherwise the
side without timestamps is aligned to the other with difflib matching blocks.
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import unicodedata

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TERMS44 = os.environ.get("MIDY_TERMS_JSON", "terms.json")
DB = os.path.join(HERE, "..", "run", "midy.db")

TS = re.compile(r"^\W*\[?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,]\d+)?\]?\W*")
SPK = re.compile(r"^\**\s*([A-Za-z][^:\n]{0,40}?)\s*:\**\s+")


def parse(path):
    """-> list of {"t": seconds|None, "speaker": str|None, "text": str}; header/empty/italic-only lines skipped."""
    rows = []
    for raw in open(path, encoding="utf-8", errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("_") and line.endswith("_")):
            continue
        t = None
        m = TS.match(line)
        if m:
            h, mi, s = m.groups(); t = (int(h) if h else 0) * 3600 + int(mi) * 60 + int(s); line = line[m.end():]
        spk = None
        m = SPK.match(line)
        if m:
            spk = m.group(1).strip("* "); line = line[m.end():]
        line = line.strip("* ").strip()
        if line:
            rows.append({"t": t, "speaker": spk, "text": line})
    return rows


def norm_words(text):
    t = unicodedata.normalize("NFC", text).lower()
    return re.findall(r"[^\W_]+", t)


def levenshtein(a, b):
    """Word-level edit distance, rows vectorised with numpy (the along-row dependency solved by a cumulative-min trick)."""
    if not a or not b:
        return max(len(a), len(b))
    vocab = {}
    A = np.array([vocab.setdefault(w, len(vocab)) for w in a], dtype=np.int32)
    B = np.array([vocab.setdefault(w, len(vocab)) for w in b], dtype=np.int32)
    n = len(B)
    prev = np.arange(n + 1, dtype=np.int32)
    idx = np.arange(n + 1, dtype=np.int32)
    for i in range(1, len(A) + 1):
        sub = (B != A[i - 1]).astype(np.int32)
        m = np.empty(n + 1, dtype=np.int32)
        m[0] = i
        m[1:] = np.minimum(prev[1:] + 1, prev[:-1] + sub)          # deletion of a[i] / substitution
        cur = np.minimum.accumulate(m - idx) + idx                 # insertions: min over k<=j of m[k] + (j - k)
        prev = cur
    return int(prev[-1])


def wer(ref, hyp):
    return round(levenshtein(ref, hyp) / max(1, len(ref)), 4)


def align_chunks(a_rows, b_rows, a_words, b_words, chunk_s):
    """Return list of (a_chunk_words, b_chunk_words) per chunk of `chunk_s` seconds of the side that has timestamps."""
    ta = [r["t"] for r in a_rows if r["t"] is not None]; tb = [r["t"] for r in b_rows if r["t"] is not None]
    if ta and tb:                                             # both timed: chunk each by its own timestamps
        def by_time(rows):
            out = {}
            for r in rows:
                if r["t"] is None:
                    continue
                out.setdefault(int(r["t"] // chunk_s), []).extend(norm_words(r["text"]))
            return out
        ca, cb = by_time(a_rows), by_time(b_rows)
        keys = sorted(set(ca) | set(cb))
        return [(ca.get(k, []), cb.get(k, [])) for k in keys], "both_timed"
    timed, other, tw, ow = (a_rows, b_rows, a_words, b_words) if ta else (b_rows, a_rows, b_words, a_words)
    if not (ta or tb):
        return [], "no_timestamps"
    # word index ranges of the timed side per chunk
    ranges, pos = {}, 0
    for r in timed:
        w = norm_words(r["text"])
        if r["t"] is not None:
            k = int(r["t"] // chunk_s); ranges.setdefault(k, [pos, pos])
            ranges[k][1] = pos + len(w)
        pos += len(w)
    # map timed word positions -> other side positions via matching blocks
    sm = difflib.SequenceMatcher(None, tw, ow, autojunk=False)
    anchors = [(b.a, b.b) for b in sm.get_matching_blocks() if b.size > 0]
    if not anchors:
        return [], "no_alignment"
    xs = np.array([x for x, _ in anchors]); ys = np.array([y for _, y in anchors])

    def to_other(p):
        j = int(np.searchsorted(xs, p, side="right")) - 1
        return int(ys[max(0, j)] + (p - xs[max(0, j)])) if j >= 0 else 0
    out = []
    for k in sorted(ranges):
        a0, a1 = ranges[k]
        o0, o1 = to_other(a0), to_other(a1)
        pair = (tw[a0:a1], ow[o0:o1]) if ta else (ow[o0:o1], tw[a0:a1])
        out.append(pair)
    return out, "aligned_to_" + ("A" if ta else "B")


def term_counts(words_joined, terms):
    return {t: len(re.findall(r"\b" + re.escape(t.lower()) + r"\b", words_joined)) for t in terms}


ap = argparse.ArgumentParser()
ap.add_argument("a", help="Midy transcript"); ap.add_argument("b", help="other transcript")
ap.add_argument("--chunk-min", type=float, default=5.0)
ap.add_argument("--bad-wer", type=float, default=0.3)
ap.add_argument("--glossary", default=None, help="JSON with {'terms': [...]} (default: Midy SQLite glossary, all spaces)")
A = ap.parse_args()

ra, rb = parse(A.a), parse(A.b)
wa = [w for r in ra for w in norm_words(r["text"])]
wb = [w for r in rb for w in norm_words(r["text"])]
res = {"a_lines": len(ra), "b_lines": len(rb), "a_words": len(wa), "b_words": len(wb),
       "words_ratio_b_over_a": round(len(wb) / max(1, len(wa)), 3),
       "a_has_timestamps": any(r["t"] is not None for r in ra), "b_has_timestamps": any(r["t"] is not None for r in rb),
       "wer_ref_a_hyp_b": wer(wa, wb), "wer_ref_b_hyp_a": wer(wb, wa)}
chunks, mode = align_chunks(ra, rb, wa, wb, A.chunk_min * 60)
res["chunk_mode"] = mode
if chunks:
    ws = [wer(ca, cb) if ca else (1.0 if cb else 0.0) for ca, cb in chunks]
    res["chunks"] = len(chunks)
    res["chunk_wer_p50"] = round(float(np.median(ws)), 3); res["chunk_wer_max"] = round(float(max(ws)), 3)
    res["share_chunks_wer_over_%.2f" % A.bad_wer] = round(sum(w > A.bad_wer for w in ws) / len(ws), 3)
    res["share_duration_wer_over_%.2f" % A.bad_wer] = res["share_chunks_wer_over_%.2f" % A.bad_wer]   # equal-length chunks
# terms: the 44 rnd terms + Midy SAP glossary
terms44 = json.load(open(TERMS44))["sap_terms"] if os.path.exists(TERMS44) else []
if A.glossary:
    gl = json.load(open(A.glossary)).get("terms", [])
elif os.path.exists(DB):
    gl = [w for (w,) in sqlite3.connect(DB).execute("select wrong from glossary where kind='term'")]
else:
    gl = []
ja, jb = " ".join(wa), " ".join(wb)
for name, terms in (("terms44", terms44), ("glossary", gl)):
    ca, cb = term_counts(ja, terms), term_counts(jb, terms)
    res[name] = {"n_terms": len(terms), "a_present": sum(v > 0 for v in ca.values()), "b_present": sum(v > 0 for v in cb.values()),
                 "a_occurrences": sum(ca.values()), "b_occurrences": sum(cb.values()),
                 "present_in_a_only": sum(ca[t] > 0 and cb[t] == 0 for t in terms), "present_in_b_only": sum(cb[t] > 0 and ca[t] == 0 for t in terms)}
res["a_speakers"] = len({r["speaker"] for r in ra if r["speaker"]}); res["b_speakers"] = len({r["speaker"] for r in rb if r["speaker"]})
print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__" and os.environ.get("COMPARE_SELFTEST"):
    assert levenshtein("a b c".split(), "a x c d".split()) == 2 and levenshtein([], "a b".split()) == 2
    assert wer("the goods receipt is posted".split(), "the goods receipt is posted".split()) == 0.0
    print("compare self-check OK")
