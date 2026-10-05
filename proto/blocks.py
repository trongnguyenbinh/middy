"""Transcript as sentence blocks in a sliding window — the reference app protocol v2 semantics:

  event {windowOffset, window[], settled?}
  - blocks [0, windowOffset) are byte-identical to the previous emission (client keeps them);
  - `window` replaces the client's tail from windowOffset;
  - `settled` (when present) is the WHOLE prefix [0, windowOffset) and replaces it (used when old blocks change,
    e.g. a speaker label rewrite).

block = one VAD sentence of one source: listening -> sentence_final (pass 1, ~0.9 s after the speaker stops)
-> refined (pass 2 rewrote it; it is emitted inside the window once) -> frozen (left the window).
Sentence-join rule (§5.2): a block whose text does not end with . ? ! is extended by the next segment of the
same source if that segment starts < JOIN_GAP_S later.
All public methods take one lock (they are called from the ASR loop, the diarization thread and the summary timer).
"""
import difflib
import re
import threading

JOIN_GAP_S = 1.0
END_PUNCT = (".", "?", "!", "。", "？", "！")
WORDS_PER_LINE = 30          # the reference app: new line when >= 30 words AND last word ends a sentence


def words(t):
    return re.findall(r"\S+", t)


def norm_words(t):
    """Case- and punctuation-insensitive word list (rnd M1 item 7)."""
    return [w for w in re.findall(r"[^\W_]+", t.lower())]


class Transcript:
    def __init__(self, on_window=None):
        self.blocks = []            # dicts: id, source, s, e, speech_end, text, text_p1, state, speaker, n_joined, emitted
        self.interim = {}           # source -> text of the line being listened to
        self.window_offset = 0
        self.on_window = on_window
        self.lock = threading.RLock()
        self.stats = {"blocks": 0, "joined": 0, "rewritten": 0, "rewritten_norm": 0, "rewritten_2plus_words": 0, "refined": 0, "summarised": 0,
                      "fallback_whole_group": 0, "word_change_ratios": [], "settled_events": 0}

    # ---- pass 1 --------------------------------------------------------------------------------------------------
    def listening(self, source, text):
        with self.lock:
            self.interim[source] = text

    def sentence_final(self, source, s, e, speech_end, text, speaker=""):
        with self.lock:
            self.interim.pop(source, None)
            last = self.blocks[-1] if self.blocks else None
            if last and last["source"] == source and last["state"] == "sentence_final" and text and \
                    not last["text"].rstrip().endswith(END_PUNCT) and s - last["e"] < JOIN_GAP_S:
                last["text"] = (last["text"] + " " + text).strip(); last["text_p1"] = last["text"]
                last["e"], last["speech_end"], last["n_joined"] = e, speech_end, last["n_joined"] + 1
                self.stats["joined"] += 1
                b = last
            else:
                b = {"id": len(self.blocks), "source": source, "s": s, "e": e, "speech_end": speech_end, "text": text, "text_p1": text,
                     "state": "sentence_final", "speaker": speaker, "n_joined": 0, "emitted": False}
                self.blocks.append(b); self.stats["blocks"] += 1
            self._emit()
            return b

    # ---- pass 2 --------------------------------------------------------------------------------------------------
    def refine(self, source, g_s, g_e, text, speaker="", dropped=False):
        """Rewrite the blocks of group [g_s, g_e) of `source` with the refined text; returns the ids touched."""
        with self.lock:
            rows = [b for b in self.blocks if b["source"] == source and b["s"] < g_e and b["e"] > g_s and b["state"] != "frozen"]
            if not rows:
                return []
            for b in rows:
                b["speaker"] = speaker or b["speaker"]
            if dropped:
                for b in rows:
                    b["text"], b["state"], b["dropped"], b["emitted"] = "", "refined", True, False
                self._emit(); return [b["id"] for b in rows]
            parts = self._split(rows, text)
            if parts is None:                                   # option (a): one merged block
                self.stats["fallback_whole_group"] += 1
                first = rows[0]
                first["text"], first["e"], first["speech_end"] = text, rows[-1]["e"], rows[-1]["speech_end"]
                first["state"], first["emitted"] = "refined", False
                for b in rows[1:]:
                    self.blocks.remove(b)
                for i, b in enumerate(self.blocks):
                    b["id"] = i
                self.stats["rewritten"] += 1; self.stats["rewritten_norm"] += 1; self.stats["rewritten_2plus_words"] += 1; self.stats["refined"] += 1
                self.stats["word_change_ratios"].append(1.0)
                self._emit()
                return [first["id"]]
            for b, new in zip(rows, parts):
                r = 1 - difflib.SequenceMatcher(None, words(b["text"]), words(new)).ratio()
                self.stats["word_change_ratios"].append(round(r, 3))
                if new != b["text"]:
                    self.stats["rewritten"] += 1
                a, c = norm_words(b["text"]), norm_words(new)
                if a != c:
                    self.stats["rewritten_norm"] += 1
                    sm = difflib.SequenceMatcher(None, a, c)
                    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
                    if changed >= 2:
                        self.stats["rewritten_2plus_words"] += 1
                b["text"], b["state"], b["emitted"] = new, "refined", False
                self.stats["refined"] += 1
            self._emit()
            return [b["id"] for b in rows]

    @staticmethod
    def _split(rows, text):
        """Distribute pass-2 words over the sentence blocks by aligning with the pass-1 words. None when alignment is poor."""
        if len(rows) == 1:
            return [text]
        p1, owner = [], []
        for i, b in enumerate(rows):
            w = words(b["text"]); p1 += w; owner += [i] * len(w)
        p2 = words(text)
        if not p1 or not p2:
            return None
        sm = difflib.SequenceMatcher(None, [w.lower() for w in p1], [w.lower() for w in p2])
        if sm.ratio() < 0.5:
            return None
        assign = [None] * len(p2)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            for j in range(j1, j2):
                if i2 > i1:
                    k = i1 + int((j - j1) * (i2 - i1) / max(1, j2 - j1))
                    assign[j] = owner[min(k, len(owner) - 1)]
        last = None
        for j in range(len(assign)):
            if assign[j] is None:
                assign[j] = last if last is not None else 0
            last = assign[j]
        out = [[] for _ in rows]
        for w, o in zip(p2, assign):
            out[o].append(w)
        if any(not o for o in out):
            return None
        return [" ".join(o) for o in out]

    # ---- window ----------------------------------------------------------------------------------------------------
    def _emit(self, settled=False):
        """the reference app v2: freeze only refined blocks that were already shown inside a window; emit; then mark as shown."""
        i = self.window_offset
        while i < len(self.blocks) and self.blocks[i]["state"] == "refined" and self.blocks[i]["emitted"]:
            self.blocks[i]["state"] = "frozen"; i += 1
        self.window_offset = i
        ev = {"type": "transcript_window", "windowOffset": self.window_offset,
              "window": [self.view(b) for b in self.blocks[self.window_offset:]], "interim": dict(self.interim)}
        if settled:
            ev["settled"] = [self.view(b) for b in self.blocks[:self.window_offset]]      # the WHOLE prefix
            self.stats["settled_events"] += 1
        for b in self.blocks[self.window_offset:]:
            if b["state"] == "refined":
                b["emitted"] = True
        if self.on_window:
            self.on_window(ev)

    def take_unsummarised(self):
        """Refined/frozen blocks with text that were never handed to the live summary; marks them. A per-block mark, not a
        time cursor: pass 2 of the mic and the system stream arrive out of time order (rnd M1 v2 item 4)."""
        with self.lock:
            rows = sorted((b for b in self.blocks if b["state"] in ("refined", "frozen") and b["text"] and not b.get("dropped") and not b.get("summarised")),
                          key=lambda b: (b["s"], b["source"]))
            for b in rows:
                b["summarised"] = True
            self.stats["summarised"] += len(rows)
            return rows

    def unsummarise(self, rows):
        """Give blocks back (the summary that consumed them was discarded, e.g. truncated output)."""
        with self.lock:
            for b in rows:
                b["summarised"] = False
            self.stats["summarised"] -= len(rows)

    def unsummarised_left(self):
        with self.lock:
            return sum(1 for b in self.blocks if b["state"] in ("refined", "frozen") and b["text"] and not b.get("dropped") and not b.get("summarised"))

    def snapshot(self):
        """For a client joining mid-meeting (the reference app: formatted_transcript / hydrate): the whole transcript as one window."""
        with self.lock:
            return {"type": "transcript_window", "windowOffset": 0, "window": [self.view(b) for b in self.blocks],
                    "interim": dict(self.interim), "snapshot": True}

    def relabel_span(self, source, s, e, speaker):
        """Set the speaker of the blocks of `source` overlapping [s, e). Frozen blocks changed -> `settled` (whole prefix)."""
        with self.lock:
            changed_frozen = False
            for b in self.blocks:
                if b["source"] == source and b["s"] < e and b["e"] > s and b["speaker"] != speaker:
                    b["speaker"] = speaker
                    changed_frozen |= b["state"] == "frozen"
            self._emit(settled=changed_frozen)

    @staticmethod
    def view(b):
        return {"id": b["id"], "source": b["source"], "s": round(b["s"], 2), "e": round(b["e"], 2), "speaker": b["speaker"], "text": b["text"],
                "isFinal": True, "state": b["state"]}

    def lines(self):
        """the reference app display rule: join consecutive blocks of one speaker+source; break at >= 30 words ending with . ? !"""
        with self.lock:
            return self._lines(self.blocks)

    @staticmethod
    def _lines(blocks):
        out, cur = [], None
        for b in blocks:
            if b.get("dropped") or not b["text"]:
                continue
            if cur and cur["speaker"] == b["speaker"] and cur["source"] == b["source"]:
                cur["text"] += " " + b["text"]
            else:
                if cur:
                    out.append(cur)
                cur = {"speaker": b["speaker"], "source": b["source"], "text": b["text"]}
            if len(words(cur["text"])) >= WORDS_PER_LINE and cur["text"].rstrip().endswith(END_PUNCT):
                out.append(cur); cur = None
        if cur:
            out.append(cur)
        return out


class WindowClient:
    """Ten-line sliding-window client: what a v2 client would display."""
    def __init__(self):
        self.entries = []
        self.warnings = 0

    def apply(self, ev):
        if ev.get("snapshot"):
            self.entries = list(ev["window"]); return
        off = ev["windowOffset"]
        if "settled" in ev:
            if len(ev["settled"]) != off:
                self.warnings += 1
            self.entries = list(ev["settled"]) + list(ev["window"])
        else:
            if len(self.entries) < off:
                self.warnings += 1
            self.entries = self.entries[:off] + list(ev["window"])

    def text(self):
        return [(e["speaker"], e["text"]) for e in self.entries if e["text"]]


if __name__ == "__main__":  # self-check, including a reference-style client replaying the events
    ev = []
    t = Transcript(on_window=ev.append)
    c = WindowClient()
    t.sentence_final("system", 0, 3, 2.8, "we open the purchasing api")
    t.sentence_final("system", 3.4, 6, 5.8, "and then the goods receipt.")          # joined: no end punctuation, gap 0.4 s
    assert len(t.blocks) == 1 and t.stats["joined"] == 1
    t.sentence_final("system", 8, 12, 11.8, "Next we post the invoice.")
    t.sentence_final("mic", 12.5, 14, 13.8, "ok")
    ids = t.refine("system", 0, 12.2, "We open the Purchasing A/P and then the goods receipt. Next we post the invoice.", speaker="Speaker 1")
    assert ids == [0, 1] and t.blocks[1]["text"] == "Next we post the invoice."
    assert ev[-1]["windowOffset"] == 0 and ev[-1]["window"][0]["text"].startswith("We open the Purchasing A/P"), "refined text must be shown inside the window (1a)"
    t.refine("mic", 12.5, 14, "OK.")
    assert ev[-1]["windowOffset"] == 2 and [w["text"] for w in ev[-1]["window"]] == ["OK."], "previous refined blocks freeze only after being shown"
    assert t.blocks[0]["state"] == "frozen" and t.blocks[1]["state"] == "frozen"
    t.sentence_final("system", 20, 22, 21.8, "later.")
    t.relabel_span("system", 0, 12.2, "Speaker 2")
    assert "settled" in ev[-1] and len(ev[-1]["settled"]) == ev[-1]["windowOffset"] == 3, "settled = whole prefix (1b)"
    assert ev[-1]["settled"][0]["speaker"] == "Speaker 2"
    for e in ev:
        c.apply(e)
    assert c.warnings == 0 and c.text() == [(b["speaker"], b["text"]) for b in t.blocks if b["text"]], "the reference app client must end with the same text"
    assert t.stats["rewritten"] == 2 and t.stats["rewritten_norm"] == 1 and t.stats["rewritten_2plus_words"] == 1   # "ok"->"OK." counts as rewritten, not as content
    t2 = Transcript(); t2.sentence_final("system", 0, 2, 1.8, "goods receipt done"); t2.refine("system", 0, 2, "Goods receipt done.")
    assert t2.stats["rewritten"] == 1 and t2.stats["rewritten_norm"] == 0, "case/punctuation-only change is not a content change"
    t3 = Transcript(); t3.sentence_final("system", 0, 2, 1.8, "a b c"); t3.sentence_final("system", 3.5, 4, 3.8, "d e f"); t3.sentence_final("system", 9, 10, 9.8, "later")
    assert t3.refine("system", 0, 4.5, "x y z q w") == [0] and len(t3.blocks) == 2 and t3.blocks[1]["id"] == 1 and t3.stats["fallback_whole_group"] == 1
    assert Transcript._split([{"text": "a b c"}, {"text": "d e f"}], "x y z q w") is None
    snap = t.snapshot(); c2 = WindowClient(); c2.apply(snap); assert c2.text() == c.text()
    # rnd M1 v2 item 4: two streams, pass 2 out of time order -> every refined block reaches the summary exactly once
    t4 = Transcript()
    t4.sentence_final("system", 90, 100, 99.8, "system talks first.")          # its group stays open (< 15 s, no 2 s gap yet)
    t4.sentence_final("mic", 101, 104, 103.8, "mic cuts in.")
    t4.refine("mic", 101, 104, "Mic cuts in.", speaker="You")                 # mic group closed early: pass 2 arrives first
    sent = [(b["source"], b["s"]) for b in t4.take_unsummarised()]
    cursor = 104                                                               # the old time cursor after this round
    t4.sentence_final("system", 105, 121, 120.8, "system goes on.")
    t4.refine("system", 90, 121, "System talks first. System goes on.", speaker="Speaker 1")
    by_cursor = [(b["source"], b["s"]) for b in t4.blocks if b["state"] in ("refined", "frozen") and b["e"] > cursor]
    assert by_cursor == [("system", 105)], "the old cursor rule drops system 90-100 (this is the bug the mark fixes)"
    sent += [(b["source"], b["s"]) for b in t4.take_unsummarised()]
    assert sent == [("mic", 101), ("system", 90), ("system", 105)], sent
    assert t4.take_unsummarised() == [] and t4.unsummarised_left() == 0 and t4.stats["summarised"] == 3
    t4.unsummarise([t4.blocks[0]]); assert t4.unsummarised_left() == 1 and [b["s"] for b in t4.take_unsummarised()] == [90]
    print("blocks self-check OK (the reference app client in sync, %d events)" % len(ev))
