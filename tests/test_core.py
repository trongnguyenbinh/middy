"""core.Session logic that needs no worker: pub/sub backlog, language switch, the Gemma event reader (live notes, Ask,
MoM passes) and the wait that must end when the Gemma worker dies."""
import io
import json
import threading

import numpy as np
import pytest

import core


@pytest.fixture
def s(tmp_path):
    ses = core.Session(name="t", run_dir=str(tmp_path / "run" / "t"), db=str(tmp_path / "t.db"))
    ses.meeting_id = ses.store.new_meeting("meeting 1", "default", "English", {"start": 0.0})
    ses.llm_events = io.StringIO()
    yield ses
    ses.blocks_fd.close(); ses.window_fd.close()


def test_init_creates_private_run_dir(s, tmp_path):
    assert (tmp_path / "run" / "t").stat().st_mode & 0o777 == 0o700


def test_subscribe_gets_snapshot_then_events(s):
    q = s.subscribe()
    assert q.get_nowait()["snapshot"] is True
    s.publish({"type": "x"})
    assert q.get_nowait() == {"type": "x"}


def test_slow_subscriber_is_told_to_resync(s):
    q = s.subscribe()
    for i in range(q.maxsize + 5):
        s.publish({"type": "transcript", "i": i})
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert {"type": "resync"} in items and len(items) < q.maxsize                # backlog dropped, memory bounded


def test_set_language(s):
    q = s.subscribe(); q.get_nowait()
    assert s.set_language("Klingon") == {"ok": False, "error": "unknown language"}
    assert s.set_language("Vietnamese") == {"ok": True, "language": "Vietnamese", "previous": "English"}
    assert open(f"{s.run_dir}/asr_language").read() == "Vietnamese"
    assert s.store.meeting(s.meeting_id)["language"] == "Vietnamese"
    assert q.get_nowait() == {"type": "language", "language": "Vietnamese", "previous": "English"}


def test_resume_of_unknown_meeting_raises_even_with_python_O(tmp_path):
    ses = core.Session(name="r", run_dir=str(tmp_path / "r"), db=str(tmp_path / "t.db"), resume=12345)
    with pytest.raises(ValueError, match="unknown meeting id"):
        ses._prepare()


def test_chunk_bounds_and_system_audio(s):
    s.origin = 100.0
    assert s._chunk_bounds(0) == (100.0, 700.0) and s._chunk_bounds(2) == (1300.0, 1900.0)
    s.audio = None
    s.sys_audio = [np.ones(core.SR, np.float32), np.zeros(core.SR, np.float32)]
    x = s._system_audio(0.5, 1.5)
    assert len(x) == core.SR and x[: core.SR // 2].all() and not x[core.SR // 2:].any()
    s.audio = np.arange(10 * core.SR, dtype=np.float32); s.cfg["start"] = 5.0
    assert s._system_audio(6.0, 7.0)[0] == core.SR


def test_status_counts_online_speakers(s):
    s.finals = [{"speaker": "Speaker 1"}, {"speaker": "Speaker 2"}, {"speaker": "Speaker 1"}, {"speaker": ""}]
    st = s.status()
    assert st["speakers_online"] == 2 and st["state"] == "init" and st["audio_s"] == 0


def dead_thread():
    t = threading.Thread(target=lambda: None); t.start(); t.join()
    return t


def test_gen_returns_the_answer(s):
    sent = []

    def answer(o):
        sent.append(o)
        s.llm_done[o["id"]] = {"text": "- point", "stats": {"finish": "stop"}}
    s._llm_send = answer
    s.llm_reader_t = threading.Thread(target=threading.Event().wait, args=(5,), daemon=True); s.llm_reader_t.start()
    assert s._gen("mcmap0", "prompt", 100) == ("- point", {"finish": "stop"})
    assert sent[0]["max_tokens"] == 100 and "mcmap0" not in s.llm_done


def test_gen_raises_when_the_worker_is_gone(s):
    s._llm_send = lambda o: None
    s.llm_reader_t = dead_thread()                 # socket closed: no answer can ever come
    with pytest.raises(RuntimeError, match="stopped before answering"):
        s._gen("mcred0", "prompt", 100)


def test_wait_llm_ends_when_condition_clears(s):
    s.llm_reader_t = dead_thread()
    s._wait_llm(lambda: False)                     # nothing to wait for: no error even with the reader gone


def feed_reader(s, events):
    s.llm_r = io.StringIO("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    s._llm_reader()


def test_reader_live_notes_are_stamped_from_the_transcript(s):
    q = s.subscribe(); q.get_nowait()
    s.finals = [{"s": 65.0, "text": "đơn mua hàng PO tạo bằng ME21N cho nhà cung cấp", "dropped_lang": False}]
    s.sum_inflight = True
    feed_reader(s, [{"event": "done", "id": "live0", "text": "- Tạo PO bằng ME21N cho nhà cung cấp [99:99]", "stats": {"finish": "stop"}}])
    note = s.store.notes(s.meeting_id, "live")[0]
    assert note["text"] == "- [01:05] Tạo PO bằng ME21N cho nhà cung cấp"
    assert s.live_note == "- Tạo PO bằng ME21N cho nhà cung cấp" and s.sum_inflight is False
    assert q.get_nowait()["kept"] is True and json.loads(s.llm_events.getvalue())["id"] == "live0"


def test_reader_drops_a_first_truncated_live_note_and_gives_blocks_back(s):
    s.live_note = "old"
    s.tx.sentence_final("system", 0, 2, 1.8, "a b c."); s.tx.refine("system", 0, 2, "A b c.")
    s.sum_rows = s.tx.take_unsummarised()
    assert s.tx.unsummarised_left() == 0
    feed_reader(s, [{"event": "done", "id": "live0", "text": "- cut", "stats": {"finish": "length"}}])
    assert s.live_note == "old" and s.live_truncated == 1 and s.tx.unsummarised_left() == 1
    assert s.store.notes(s.meeting_id, "live") == []
    feed_reader(s, [{"event": "done", "id": "live1", "text": "- cut again", "stats": {"finish": "length"}}])
    assert s.live_note == "- cut again" and s.live_truncated_kept == 1          # the second one in a row is kept


def test_reader_ask_streams_and_saves(s):
    q = s.subscribe(); q.get_nowait()
    s.finals = [{"s": 130.0, "text": "nhập kho bằng MIGO", "dropped_lang": False}]
    s.asks["ask0"] = {"q": "Nhập kho bằng gì?", "t_sent": 0}
    feed_reader(s, [{"event": "delta", "id": "ask0", "text": "MIGO"},
                    {"event": "done", "id": "ask0", "text": "MIGO [02:10] [07:77]", "stats": {"gen_tokens": 3}}])
    assert [q.get_nowait()["type"] for _ in range(2)] == ["ask_delta", "ask_done"]
    saved = json.loads(s.store.notes(s.meeting_id, "ask")[0]["text"])
    assert saved == {"q": "Nhập kho bằng gì?", "a": "MIGO [02:10]"}                # a time not in the transcript is removed
    assert "t_first" in s.asks["ask0"] and "delta" not in s.llm_events.getvalue()


def test_reader_mom_passes_wait_in_llm_done(s):
    feed_reader(s, [{"event": "done", "id": "mcmap0", "text": "- x", "stats": {}}, {"event": "progress", "id": "mcmap0", "tokens": 25}])
    assert s.llm_done["mcmap0"]["text"] == "- x" and s.store.notes(s.meeting_id) == []


def test_ask_needs_a_question_and_a_ready_model(s):
    assert s.ask("  ") == {"ok": False, "error": "empty question"}
    assert s.ask("what?") == {"ok": False, "error": "the notes model is still loading"}
    s.state = "done"
    assert s.ask("what?") == {"ok": False, "error": "no meeting"}


def test_ask_sends_a_streamed_generation(s):
    sent = []
    s.state, s.llm, s._llm_send = "recording", object(), sent.append
    s.finals = [{"s": 1.0, "speaker": "Speaker 1", "text": "goods receipt", "dropped_lang": False}]
    r = s.ask("goods?")
    assert r["ok"] and r["id"] == "ask0" and sent[0]["stream"] is True and "goods receipt" in sent[0]["messages"][0]["content"]


def test_pause_combines_own_and_external(s):
    sent, hooks = [], []
    s.llm, s._llm_send, s.on_pause = object(), sent.append, hooks.append
    s._set_pause(True); s._set_pause(ext=True); s._set_pause(False)
    assert [o["cmd"] for o in sent] == ["pause"] and hooks == [True, False] and s.llm_paused     # ext keeps it paused
    s._set_pause(ext=False)
    assert [o["cmd"] for o in sent] == ["pause", "resume"] and not s.llm_paused


def test_llm_send_skips_pause_to_a_leaving_worker(s):
    s.llm_w = io.StringIO()
    s.llm_quit = True
    s._llm_send({"cmd": "pause"})
    s._llm_send({"cmd": "generate", "id": "mom"})
    assert s.llm_w.getvalue().count("\n") == 1 and "mom" in s.llm_sent

