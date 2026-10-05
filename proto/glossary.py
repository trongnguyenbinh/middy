"""Term glossary (design 1.7), language filter (1.4), ASR context builder (1.5/1.7).

Everything here must be deterministic code, never the LLM (design 1.6 "general rule").
"""
import json
import re

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9/_\-]{1,}")
MAX_CONTEXT_WORDS = 6
CJK_DROP_RATIO = 0.3
# Letters only Vietnamese uses. Qwen3-ASR keeps writing Vietnamese even when forced to English (public B1: 0.269 both ways),
# so this ratio tells a Vietnamese meeting when the user forgot to pick the language (Lỗi 11).
VI = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹáàãéèíìóòõúùý]", re.I)
VI_OUTPUT_RATIO = 0.1   # ponytail: pure Vietnamese ~0.27, English ~0; ~0.1 = about a third spoken Vietnamese. Vietnamese only.

try:
    EN_WORDS = {w.strip().lower() for w in open("/usr/share/dict/words")}
except OSError:
    EN_WORDS = set()


class Glossary:
    def __init__(self, path):
        d = json.load(open(path))
        self.corrections = {k.lower(): v for k, v in d.get("corrections", {}).items()}
        self.terms = list(d.get("terms", []))
        self.ambiguous = {a.lower() for a in d.get("ambiguous", [])}
        # whole-word, case-insensitive; longest key first so multi-word entries win
        keys = sorted(self.corrections, key=len, reverse=True)
        self.rx = re.compile(r"(?<![\w/])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w/])", re.I) if keys else None

    def correct(self, text):
        """Returns (corrected_text, n_replacements). Whole-word only; ambiguous phrases are never replaced."""
        if not self.rx:
            return text, 0
        n = 0

        def sub(m):
            nonlocal n
            k = m.group(1).lower()
            if k in self.ambiguous:
                return m.group(0)
            n += 1
            return self.corrections[k]
        return self.rx.sub(sub, text), n

    def context_terms(self):
        return self.terms[:MAX_CONTEXT_WORDS]


def cjk_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return len(CJK.findall(text)) / len(letters) if letters else 0.0


def language_drop(text, meeting_language):
    """Design 1.4 first version (zero cost): Latin-script meeting -> drop group if CJK ratio > 0.3."""
    if meeting_language.lower() in ("chinese", "japanese", "korean"):
        return False
    return cjk_ratio(text) > CJK_DROP_RATIO


def output_language(texts, chosen):
    """Lỗi 11: notes and MoM are written in the chosen language, unless the recognised text is clearly Vietnamese."""
    letters = sum(c.isalpha() for t in texts for c in t)
    vi = sum(len(VI.findall(t)) for t in texts)
    return "Vietnamese" if letters and vi / letters >= VI_OUTPUT_RATIO else chosen


def slide_terms(lines, n=MAX_CONTEXT_WORDS):
    """Rank slide OCR words: code (letters + digits, e.g. AB12) > UPPERCASE 2-6 > word not in the English dictionary."""
    score = {}
    for t in WORD.findall(" ".join(lines)):
        d = 3 if re.fullmatch(r"[A-Z]{2,5}\d{1,4}[A-Z]?", t) else 2 if re.fullmatch(r"[A-Z]{2,6}", t) else \
            1 if len(t) >= 3 and t.lower() not in EN_WORDS else 0
        if d:
            k = t.lower()
            prev = score.get(k, (0, 0, t))
            score[k] = (max(prev[0], d), prev[1] + 1, prev[2])
    return [v[2] for v in sorted(score.values(), key=lambda v: (-v[0], -v[1]))[:n]]


def build_context(glossary_terms, slide_words, n=MAX_CONTEXT_WORDS):
    seen, out = set(), []
    for w in list(glossary_terms) + list(slide_words):
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
        if len(out) == n:
            break
    return " ".join(out)


def context_leak(text, context, run=3):
    """True if >= `run` consecutive context words appear in order in the text (design 1.7 leak filter)."""
    ct = context.lower().split()
    if len(ct) < run:
        return False
    tx = " ".join(re.findall(r"[a-z0-9][a-z0-9/\-]*", text.lower()))
    return any(" ".join(ct[i:i + run]) in tx for i in range(len(ct) - run + 1))


if __name__ == "__main__":  # self-check
    import tempfile
    import os
    p = tempfile.mktemp(suffix=".json")
    json.dump({"corrections": {"marketing ab": "Marketing A/B", "cả ba": "KPI"}, "terms": ["QR", "Marketing Plan"],
               "ambiguous": ["cả ba"]}, open(p, "w"))
    g = Glossary(p); os.unlink(p)
    assert g.correct("open marketing ab now")[0] == "open Marketing A/B now"
    assert g.correct("Marketing AB.")[1] == 1
    assert g.correct("marketing abs")[1] == 0, "substring must not match"
    assert g.correct("Cả ba đi họp")[0] == "Cả ba đi họp", "ambiguous phrase must never be replaced"
    assert language_drop("这是中文 内容 很多", "English") and not language_drop("hello QR world", "English")
    assert not language_drop("这是中文", "Chinese")
    assert abs(cjk_ratio("ab这") - 1 / 3) < 1e-9
    assert output_language(["hôm nay mình sẽ hướng dẫn làm đơn hàng QR"], "English") == "Vietnamese"
    assert output_language(["today we post the budget plan in QR", "café"], "English") == "English"
    assert output_language([], "German") == "German" and output_language([""], "English") == "English"
    assert slide_terms(["Budget Plan QR", "Marketing Plan FY2026 the and"])[:2] == ["FY2026", "QR"]
    assert build_context(["QR", "Marketing"], ["qr", "FY2026", "a", "b", "c", "d", "e"]) == "QR Marketing FY2026 a b c"
    assert context_leak("we use budget plan qr here", "budget plan qr") and not context_leak("budget qr plan", "budget plan qr")
    print("glossary self-check OK")
