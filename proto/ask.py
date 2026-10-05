"""Việc 17: "Ask anything" during a meeting — the context sent to the local Gemma (deterministic code, design 1.6).
the daemon builds it:
the live notes (<= 12 lines) + the most RECENT transcript + the older sentences that share words with the question,
both capped in words so a 60-minute meeting still costs a few seconds of prefill (measured in the Việc 17 report).
"""
import re

from glossary import VI

RECENT_WORDS = 1500      # ponytail: word overlap, not embeddings; add vector search if answers miss paraphrased content
MATCH_WORDS = 1500
STOP = set("""the a an and or of to in on for is are was were be been it this that what which who when where how why do does did
can could would should will i you we they he she me my our your about with from at by as not no yes please tell say said
và của là có không được cho các những một này đó thì mà với trong ra vào lại đã đang sẽ rồi cũng như để khi nào gì ai sao
bao nhiêu thế nào hả nhé ạ vậy anh chị em bạn mình họ""".split())
WORD = re.compile(r"[^\W_]+", re.U)


def words(t):
    return WORD.findall(t.lower())


def mmss(s):
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def context(rows, question, recent_words=RECENT_WORDS, match_words=MATCH_WORDS):
    """rows: [(start_s, speaker, text)] in time order. Returns (transcript excerpt, n_rows_used, n_words_used)."""
    rows = [r for r in rows if r[2].strip()]
    keep, n = set(), 0
    for i in range(len(rows) - 1, -1, -1):                      # most recent first, until the budget is spent
        if n >= recent_words:
            break
        keep.add(i); n += len(words(rows[i][2]))
    q = {w for w in words(question) if w not in STOP and (len(w) > 1 or w.isdigit())}
    scored = sorted(((len(q & set(words(t))), i) for i, (_, _, t) in enumerate(rows) if i not in keep), reverse=True)
    m = 0
    for score, i in scored:
        if score == 0 or m >= match_words:
            break
        keep.add(i); m += len(words(rows[i][2]))
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        s, spk, t = rows[i]
        out.append(f"[{mmss(s)}] {spk or 'Speaker ?'}: {t}")
        prev = i
    return "\n".join(out), len(keep), n + m


def answer_language(question):
    """Decided by code: told only "the language of the question", Gemma answered an English question in Vietnamese when the
    transcript was Vietnamese (measured, Việc 17). ponytail: Vietnamese typed without accents counts as English."""
    if VI.search(question):
        return "Vietnamese"
    letters = [c for c in question if c.isalpha()]
    return "English" if letters and all(c.isascii() for c in letters) else "the same language as the question"


def prompt(template, notes, rows, question):
    tr, n_rows, n_words = context(rows, question)
    return (template.replace("{{NOTES}}", notes or "(none yet)").replace("{{TRANSCRIPT}}", tr or "(nothing recognised yet)")
            .replace("{{QUESTION}}", question).replace("{{ANSWER_LANGUAGE}}", answer_language(question))), n_rows, n_words


if __name__ == "__main__":
    rows = [(i * 10.0, "Speaker 1", f"filler sentence number {i} about nothing") for i in range(1000)]
    rows[5] = (50.0, "Speaker 2", "the QR zone 101 posts the budget plan")
    tr, k, n = context(rows, "Which zone posts the budget plan?", recent_words=100, match_words=50)
    assert "zone 101" in tr, "an old sentence that matches the question must be kept"
    assert "[166:30]" in tr and "…" in tr, "the most recent part is kept, with a gap marker"
    assert n <= 100 + 50 + 20
    tr2, _, _ = context(rows, "gì vậy?", recent_words=30, match_words=30)
    assert "QR" not in tr2, "stop words alone must not pull old sentences in"
    assert context([], "x") == ("", 0, 0)
    assert answer_language("Ai là giám đốc dự án?") == "Vietnamese" and answer_language("Which vendor was used?") == "English"
    assert answer_language("誰がマネージャーですか") == "the same language as the question"
    print("ask self-check OK")
