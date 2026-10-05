"""Việc 17 acceptance, blind (public audio / public text only; no app window, no speaker).
  ask_test.py live   the daemon records the public B1 file (0-240 s, real time); 4 questions are asked DURING the meeting
                     (2 answered in it, 2 not; Vietnamese and English) -> answers, timings, and whether the recording suffered
  ask_test.py long   a 64-minute meeting built from public text (B1 transcript first, then ~50 min of FLEURS vi sentences) ->
                     context size, prompt tokens and answer time for a question whose answer is only at the START
Prints numbers + the answers (public content).
"""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time

import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
run = os.path.join(HERE, "..", "run")
B1 = os.environ.get("MIDY_TEST_AUDIO", "test_audio.mp3")
QUESTIONS = [(150, "Khi tạo đơn hàng thì cần điền những loại ngày nào?"), (170, "Which vendor was used as the example?"),
             (200, "Giá trị hợp đồng của dự án này là bao nhiêu tiền?"), (220, "Who is the project manager of this meeting?")]


def live():
    sock_path, db = os.path.join(run, "l17.sock"), os.path.join(run, "l17.db")
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    wav = os.path.join(run, "l17_in.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "0", "-t", "240", "-i", B1, "-ac", "1", "-ar", "16000", wav], check=True)
    x, _ = sf.read(wav, dtype="int16")
    d = subprocess.Popen([sys.executable, os.path.join(HERE, "midyd.py"), "--socket", sock_path, "--db", db], stdout=subprocess.PIPE); d.stdout.readline()
    def conn():
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock_path); return c.makefile("r"), c.makefile("w")
    r, w = conn()
    def req(o):
        w.write(json.dumps(o, ensure_ascii=False) + "\n"); w.flush(); return json.loads(r.readline())
    while req({"cmd": "status"})["status"]["pool"] != {"asr": "warm", "llm": "warm"}:
        time.sleep(0.5)
    req({"cmd": "start", "name": "meeting 1 l17", "live": "ui", "language": "Vietnamese", "chunk_min": 10})
    deltas, done, sent = {}, {}, {}
    def sub():
        r2, w2 = conn(); w2.write('{"cmd":"subscribe"}\n'); w2.flush(); r2.readline()
        for line in r2:
            ev = json.loads(line)
            if ev.get("type") == "ask_delta":
                deltas.setdefault(ev["id"], []).append(time.time())
            elif ev.get("type") == "ask_done":
                done[ev["id"]] = (time.time(), ev["text"])
            elif ev.get("type") == "done":
                break
    threading.Thread(target=sub, daemon=True).start()
    while True:
        st = req({"cmd": "status"})["status"]
        if st.get("state") == "recording" and st.get("meeting_id"):
            break
        time.sleep(0.05)
    t0, qi, n = time.time(), 0, 4000
    for i in range(0, len(x), n):
        while qi < len(QUESTIONS) and i / 16000 >= QUESTIONS[qi][0]:
            rep = req({"cmd": "ask", "question": QUESTIONS[qi][1]}); sent[rep.get("id")] = (time.time(), QUESTIONS[qi][1], rep); qi += 1
        for stream in (0, 1):
            w.write(json.dumps({"cmd": "audio", "stream": stream, "pcm": base64.b64encode(x[i:i + n].tobytes()).decode()}) + "\n")
        w.flush(); time.sleep(max(0.0, t0 + (i + n) / 16000 - time.time()))
    deadline = time.time() + 120
    while len(done) < len(sent) and time.time() < deadline:
        time.sleep(0.5)
    req({"cmd": "stop"}); mid = st["meeting_id"]
    while req({"cmd": "meeting", "meeting_id": mid})["meeting"]["status"] in ("recording", "finishing"):
        time.sleep(1)
    m = req({"cmd": "meeting", "meeting_id": mid})
    time.sleep(2)
    stp = os.path.join(run, "meeting 1 l17", "stats.json"); st = json.load(open(stp))
    req({"cmd": "quit"})
    out = {"asks": [], "saved_ask_notes": sum(1 for n in m["notes"] if n["kind"] == "ask"), "mom": any(n["kind"] == "mom" for n in m["notes"]),
           "blocks_not_summarised": st["live_summary"]["blocks_refined_not_summarised"], "live_notes": st["live_summary"]["n"],
           "asr_queue_lag_s": st["asr"]["queue_lag_s"], "asr_lag_peaks_over_1s": len(st["asr"]["lag_peaks_over_1.0s"]),
           "silence_inserted_s": st["silence_inserted_s"], "stats_asks": st.get("asks")}
    for aid, (ts, q, rep) in sent.items():
        out["asks"].append({"q": q, "reply": {k: rep.get(k) for k in ("ok", "context_words", "error")},
                            "first_token_s": round(deltas[aid][0] - ts, 2) if aid in deltas else None,
                            "answer_s": round(done[aid][0] - ts, 2) if aid in done else None, "n_deltas": len(deltas.get(aid, [])),
                            "answer": done[aid][1] if aid in done else None})
    print(json.dumps(out, ensure_ascii=False, indent=1))


def long():
    import ask
    from core import ENV, PY, PROMPTS
    ev = [json.loads(l) for l in open(os.path.join(run, "m1_b1_b2", "events.jsonl"))]
    rows = sorted([(e["s"] - 60, "Speaker 1", e["text"]) for e in ev if e.get("type") == "final" and e["text"]], key=lambda r: r[0])
    t = rows[-1][0] + 5
    fl = [l.split("\t")[2] for l in open(os.environ["MIDY_FLEURS_VI_TSV"])]
    for i, s in enumerate(fl):                              # ~2.6 words/s of speech until minute 64
        if t > 64 * 60:
            break
        rows.append((t, f"Speaker {2 + i % 3}", s)); t += len(s.split()) / 2.6
    total_words = sum(len(r[2].split()) for r in rows)
    notes = "## Topics\n- Module walkthrough, later general discussion\n## Key points\n- None yet\n## Decisions\n- None yet\n## Action items\n- None yet"
    sock = os.path.join(run, "l17_long.sock")
    if os.path.exists(sock):
        os.unlink(sock)
    wk = subprocess.Popen([PY, os.path.join(HERE, "llm_worker.py"), "--socket", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    while not os.path.exists(sock):
        time.sleep(0.2)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock); r, w = c.makefile("r"), c.makefile("w"); r.readline()
    out = {"meeting_minutes": round(rows[-1][0] / 60, 1), "transcript_words": total_words, "questions": []}
    for q in ("Khi tạo đơn hàng thì cần điền những loại ngày nào?", "Which vendor was used as the example at the start?", "Ai là giám đốc dự án?"):
        p, n_rows, n_words = ask.prompt(PROMPTS["ask"], notes, rows, q)
        t1 = time.time(); w.write(json.dumps({"cmd": "generate", "id": "q", "messages": [{"role": "user", "content": p}], "max_tokens": 600, "stream": True}) + "\n"); w.flush()
        first = None
        while True:
            e = json.loads(r.readline())
            if e["event"] == "delta" and first is None:
                first = time.time() - t1
            if e["event"] == "done":
                break
        whole = ask.prompt(PROMPTS["ask"], notes, rows, q)[0] if False else None
        out["questions"].append({"q": q, "context_words": n_words, "context_rows": n_rows, "prompt_tokens": e["stats"]["prompt_tokens"],
                                 "first_token_s": round(first, 2), "answer_s": round(time.time() - t1, 2), "answer": e["text"]})
    full = sum(len(r_[2].split()) for r_ in rows)
    out["whole_transcript_words_if_sent"] = full
    w.write('{"cmd":"quit"}\n'); w.flush(); wk.wait(timeout=30)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if "--no-ask" in sys.argv:                               # control run: same audio, no question
    QUESTIONS.clear()
{"live": live, "long": long}[sys.argv[1]]()
