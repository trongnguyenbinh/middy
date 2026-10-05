"""Streaming VAD state machine (vad.StreamVad.feed) on a scripted speech-probability track: no ONNX model."""
import numpy as np
import pytest

import vad

W = vad.WIN / vad.SR             # 32 ms per window


class ScriptedVad(vad.StreamVad):
    def __init__(self, probs, **kw):          # skip the ONNX session; same state as the real constructor
        self.probs = iter(probs)
        self.thr, self.neg = kw.get("threshold", 0.5), max(kw.get("threshold", 0.5) - 0.15, 0.01)
        self.min_sil, self.pad = kw.get("min_silence", 0.5), kw.get("pad", 0.2)
        self.max_speech = kw.get("max_speech", 25.0) - W - 2 * self.pad
        self.n, self.speaking, self.start = 0, False, 0.0
        self.temp_end = self.prev_end = self.next_start = None

    def prob(self, win):
        return next(self.probs)


def run(probs, **kw):
    v = ScriptedVad(probs, **kw); ev = []
    for _ in probs:
        ev += v.feed(np.zeros(vad.WIN, np.float32))
    return ev + v.flush()


def secs(n):
    return int(round(n / W))


def test_one_utterance_with_padding_and_min_silence():
    p = [0.0] * secs(1.0) + [0.9] * secs(2.0) + [0.0] * secs(1.0)
    ev = run(p)
    assert [k for k, _ in ev] == ["start", "end"]
    assert ev[0][1] == pytest.approx(1.0 - 0.2, abs=W) and ev[1][1] == pytest.approx(3.0, abs=W)


def test_short_pause_does_not_split_and_hysteresis_holds():
    p = [0.9] * secs(1.0) + [0.0] * secs(0.3) + [0.4] * secs(0.5) + [0.9] * secs(1.0) + [0.0] * secs(1.0)
    assert [k for k, _ in run(p)] == ["start", "end"]          # 0.4 sits between neg (0.35) and thr (0.5): still speech


def test_max_speech_cuts_at_the_last_pause():
    p = [0.9] * secs(2.0) + [0.0] * secs(0.2) + [0.9] * secs(3.0) + [0.0] * secs(1.0)
    ev = run(p, max_speech=4.0)
    kinds = [k for k, _ in ev]
    assert kinds[:3] == ["start", "end", "start"] and ev[1][1] == pytest.approx(2.0, abs=W)


def test_hard_cut_without_pause_and_flush_while_speaking():
    ev = run([0.9] * secs(5.0), max_speech=3.0)
    assert [k for k, _ in ev] == ["start", "end", "start", "end"] and ev[1][1] == pytest.approx(3.0 - W - 0.4, abs=2 * W)
    assert run([0.9] * 10)[-1][0] == "end" and run([0.0] * 10) == []
