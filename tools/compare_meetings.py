"""Việc 20b: compare two REAL meetings BLIND — numbers only. Nothing of the transcript, notes or MoM is ever printed or written:
every value that leaves this script goes through numbers_only(), which refuses strings.
  tools/compare_meetings.py <reference_id> <candidate_id> [--gemma]     (--gemma: local Gemma scores coverage 0-10, digits only)
"""
import difflib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.realpath(os.path.join(HERE, ".."))
DB = os.path.join(ROOT, "run", "midy.db")
WORD = re.compile(r"[^\W_]+", re.U)
STOP = set("""và của là có không được cho các những một này đó thì mà với trong ra vào lại đã đang sẽ rồi cũng như để khi nào gì ai sao
bao nhiêu thế vậy anh chị em bạn mình họ ở đây kia thôi nhé ạ à ừ ờ vâng dạ nó cái con người việc làm theo từ về hay hoặc nhưng nên
the a an and or of to in on for is are was were be been it this that what which who when where how why do does did we you they i""".split())


def numbers_only(x):
    """The blind-run guard: numbers, booleans, None and containers of them pass; any other string is replaced."""
    if isinstance(x, dict):
        return {k: numbers_only(v) for k, v in x.items()}          # keys are ours (metric names), never meeting text
    if isinstance(x, (list, tuple)):
        return [numbers_only(v) for v in x]
    if isinstance(x, (int, float, bool)) or x is None:
        return x
    return "[text withheld]"


def words(t):
    return WORD.findall(t.lower())


def wer(ref, hyp):
    """Word error rate of hyp against ref (Levenshtein on words, two rows)."""
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def keywords(text, min_count=2):
    c = {}
    for w in words(text):
        if len(w) >= 3 and w not in STOP and not w.isdigit():
            c[w] = c.get(w, 0) + 1
    return {w for w, n in c.items() if n >= min_count}


def load(db, mid):
    m = db.execute("select name, language, audio_end_s, source, status from meetings where id=?", (mid,)).fetchone()
    segs = db.execute("select s, e, text, stream, dropped_lang from segments where meeting_id=? order by s", (mid,)).fetchall()
    notes = db.execute("select kind, idx, text from notes where meeting_id=? order by kind, idx", (mid,)).fetchall()
    run_dir = json.loads(m[3]).get("run_dir")
    st = json.load(open(os.path.join(run_dir, "stats.json"))) if run_dir and os.path.exists(os.path.join(run_dir, "stats.json")) else {}
    live = [t for k, i, t in notes if k == "live"]
    return {"language": m[1], "audio_s": m[2], "status": m[4], "segs": segs, "transcript": " ".join(t for _, _, t, _, d in segs if t and not d),
            "live_final": live[-1] if live else "", "n_live": len(live), "mom": next((t for k, i, t in notes if k == "mom"), ""),
            "user_note": next((t for k, i, t in notes if k == "user"), ""), "stats": st}


def mom_shape(md):
    return {"sections": len(re.findall(r"^## ", md, re.M)), "words": len(words(md)), "table_rows": max(0, len(re.findall(r"^\|", md, re.M)) - 2),
            "bullets": len(re.findall(r"^\s*[\*\-] ", md, re.M)), "none_markers": len(re.findall(r"\b(None|Không có)\b", md))}


def gemma_score(transcript, summary, what):
    """Local Gemma rates coverage 0-10; only the digits of its reply are kept."""
    sys.path.insert(0, os.path.join(ROOT, "proto"))
    from core import ENV, PY
    sock = os.path.join(ROOT, "run", "cmp20b.sock")
    if os.path.exists(sock):
        os.unlink(sock)
    w = subprocess.Popen([PY, os.path.join(ROOT, "proto", "llm_worker.py"), "--socket", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    while not os.path.exists(sock):
        time.sleep(0.2)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock); r, f = c.makefile("r"), c.makefile("w"); r.readline()
    p = (f"Below is a meeting TRANSCRIPT and its {what}. Rate from 0 to 10 how completely and correctly the {what} covers the important "
         f"content of the transcript (10 = everything important, nothing wrong). Reply with ONE integer only.\n\nTRANSCRIPT:\n{transcript[:24000]}\n\n{what.upper()}:\n{summary}")
    f.write(json.dumps({"cmd": "generate", "id": "s", "messages": [{"role": "user", "content": p}], "max_tokens": 5}) + "\n"); f.flush()
    while True:
        e = json.loads(r.readline())
        if e["event"] == "done":
            break
    f.write('{"cmd":"quit"}\n'); f.flush(); w.wait(timeout=30)
    m = re.search(r"\d+", e["text"])
    return int(m.group()) if m and 0 <= int(m.group()) <= 10 else None


def main():
    ref_id, cand_id = int(sys.argv[1]), int(sys.argv[2])
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)                  # read-only on the real database
    R, C = load(db, ref_id), load(db, cand_id)
    rw, cw = words(R["transcript"]), words(C["transcript"])
    same = {"ref_words": len(rw), "cand_words": len(cw), "wer_cand_vs_ref": round(wer(rw, cw), 3) if rw else None,
            "char_similarity": round(difflib.SequenceMatcher(None, R["transcript"], C["transcript"], autojunk=False).ratio(), 3),
            "vocab_jaccard": round(len(set(rw) & set(cw)) / max(1, len(set(rw) | set(cw))), 3)}
    out = {"same_content": same}
    for tag, M in (("ref", R), ("cand", C)):
        st, asr, ls, cap = M["stats"], M["stats"].get("asr", {}), M["stats"].get("live_summary", {}), M["stats"].get("capture", {})
        kw = keywords(M["transcript"])
        cov = lambda t: round(len(kw & set(words(t))) / max(1, len(kw)), 3)  # noqa: B023 (called right away, inside the loop)
        out[tag] = {"audio_min": round((M["audio_s"] or 0) / 60, 1), "segments": len(M["segs"]), "transcript_words": len(words(M["transcript"])),
                    "segments_dropped_lang": sum(1 for s in M["segs"] if s[4]), "silence_inserted_s": st.get("silence_inserted_s"),
                    "capture_rate_min": {k: v.get("min") for k, v in cap.get("capture_rate", {}).items()} if isinstance(cap, dict) else None,
                    "asr_queue_lag_s": asr.get("queue_lag_s"), "asr_words_final": asr.get("words_final"),
                    "live_rounds": M["n_live"], "live_gen_seconds": round(sum(p.get("wall_s", 0) for p in ls.get("per_summary", [])), 1),
                    "blocks_refined": ls.get("blocks_refined_with_text"), "blocks_not_summarised": ls.get("blocks_refined_not_summarised"),
                    "keywords": len(kw), "keyword_coverage_live_notes": cov(M["live_final"]), "keyword_coverage_mom": cov(M["mom"]),
                    "mom": mom_shape(M["mom"]), "user_edited_note": bool(M["user_note"])}
    mr, mc = words(R["mom"]), words(C["mom"])
    out["mom_similarity"] = {"unigram_f1": round(2 * len(set(mr) & set(mc)) / max(1, len(set(mr)) + len(set(mc))), 3),
                             "char_similarity": round(difflib.SequenceMatcher(None, R["mom"], C["mom"], autojunk=False).ratio(), 3)}
    kr = keywords(R["transcript"])
    out["cand_mom_coverage_of_ref_keywords"] = round(len(kr & set(mc)) / max(1, len(kr)), 3)
    if "--gemma" in sys.argv:
        out["gemma_score_0_10"] = {tag: {"live_notes": gemma_score(M["transcript"], M["live_final"], "live notes"), "mom": gemma_score(M["transcript"], M["mom"], "minutes of meeting")}
                                   for tag, M in (("ref", R), ("cand", C))}
    print(json.dumps(numbers_only(out), indent=1))


if __name__ == "__main__":
    main()
