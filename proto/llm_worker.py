"""LLM worker: Gemma 4 E4B through mlx-lm in its own process, talking over a Unix domain socket.
No HTTP, no TCP port (design 1.6 / 1.9). One client (the orchestrator). Newline-delimited JSON.

client -> worker: {"cmd":"generate","id":..,"messages":[...],"max_tokens":N,"stream":bool}   (stream: one "delta" event per token)
                  {"cmd":"pause"} {"cmd":"resume"} {"cmd":"quit"}
worker -> client: {"event":"ready",...} {"event":"start","id"} {"event":"progress","id","tokens"}
                  {"event":"delta","id","text"} {"event":"done","id","text","stats":{...}}
Pause stops between two tokens and keeps the KV cache (design 1.6).
"""
import argparse
import json
import os
import queue
import resource
import socket
import threading
import time
from pathlib import Path

import mlx.core as mx
from mlx_lm import stream_generate
from mlx_lm.sample_utils import make_sampler
from mlx_lm.utils import load_model, load_tokenizer

ap = argparse.ArgumentParser()
ap.add_argument("--socket", required=True)
ap.add_argument("--model-dir", default=os.path.join(os.path.dirname(__file__), "..", "models", "gemma-4-E4B-it-MLX-8bit"))
ap.add_argument("--temp", type=float, default=0.3)
A = ap.parse_args()

def exit_with_parent():                          # Lỗi 15: a worker kept warm waits in accept(); never outlive the daemon.
    while os.getppid() != 1:                     # orphans are re-parented to launchd (pid 1); comparing with the pid seen at
        time.sleep(2)                            # start would miss a daemon that died before this line ran
    try:
        os.remove(A.socket)
    except OSError:
        pass
    os._exit(0)


threading.Thread(target=exit_with_parent, daemon=True).start()
t0 = time.time()
p = Path(A.model_dir)
# strict=False: the LM Studio conversion ships k/v projection weights for the KV-shared layers 24-41 that gemma4 in mlx-lm does not use
model, _cfg = load_model(p, strict=False)
# generation_config.json lists three end ids (1, 106 = <turn|>, 50); the tokenizer alone only knows 1, so the model would
# never stop at end of turn and would emit <turn|> until max_tokens
gen_cfg = json.load(open(p / "generation_config.json"))
tok = load_tokenizer(p, eos_token_ids=gen_cfg.get("eos_token_id"))
sampler = make_sampler(temp=A.temp)
for _ in stream_generate(model, tok, tok.apply_chat_template([{"role": "user", "content": "hi"}], add_generation_prompt=True, enable_thinking=False), max_tokens=4, sampler=sampler):
    pass
load_s = time.time() - t0

if os.path.exists(A.socket):
    os.unlink(A.socket)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(A.socket); srv.listen(1)
conn, _ = srv.accept()
wfile = conn.makefile("w")
rfile = conn.makefile("r")
lock = threading.Lock()


def send(o):
    with lock:
        wfile.write(json.dumps(o, ensure_ascii=False) + "\n"); wfile.flush()


paused = threading.Event()   # set = paused
paused_total = 0.0
reqs = queue.Queue()


def reader():
    for line in rfile:
        m = json.loads(line)
        if m["cmd"] == "generate":
            reqs.put(m)
        elif m["cmd"] == "pause":
            paused.set()
        elif m["cmd"] == "resume":
            paused.clear()
        elif m["cmd"] == "quit":
            reqs.put(None); return
    reqs.put(None)


threading.Thread(target=reader, daemon=True).start()
def net_selftest():
    out = []
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=1.0).close(); out.append("tcp:OPEN")
    except OSError as e:
        out.append(f"tcp:blocked:{e.errno}")
    try:
        socket.getaddrinfo("apple.com", 443); out.append("dns:OPEN")
    except OSError as e:
        out.append(f"dns:blocked:{e.errno}")
    return " ".join(out)


send({"event": "ready", "load_s": round(load_s, 2), "gpu_active_gb": round(mx.get_active_memory() / 1e9, 2), "net_selftest": net_selftest()})

while True:
    m = reqs.get()
    if m is None:
        break
    prompt = tok.apply_chat_template(m["messages"], add_generation_prompt=True, enable_thinking=False)  # thinking off: notes only, no reasoning channel
    send({"event": "start", "id": m["id"], "prompt_tokens": len(prompt), "t": time.time()})
    t_start = time.time(); ttft = None; n = 0; pz = 0.0; last = None; parts = []
    for r in stream_generate(model, tok, prompt, max_tokens=m.get("max_tokens", 1200), sampler=sampler):
        n += 1; last = r; parts.append(r.text)
        if m.get("stream") and r.text:           # Việc 17: Ask answers stream token by token (the reference app text_delta)
            send({"event": "delta", "id": m["id"], "text": r.text})
        if ttft is None:
            ttft = time.time() - t_start
        if n % 25 == 0:
            send({"event": "progress", "id": m["id"], "tokens": n, "t": time.time(), "gpu_active_gb": round(mx.get_active_memory() / 1e9, 2)})
        if paused.is_set():
            tp = time.time()
            while paused.is_set():
                time.sleep(0.01)
            pz += time.time() - tp
    wall = time.time() - t_start
    paused_total += pz
    send({"event": "done", "id": m["id"], "text": "".join(parts), "t": time.time(),
          "stats": {"prompt_tokens": last.prompt_tokens, "gen_tokens": n, "ttft_s": round(ttft, 2), "wall_s": round(wall, 2),
                    "paused_s": round(pz, 2), "active_tps": round(n / max(1e-6, wall - pz - ttft), 1),
                    "mlx_gen_tps": round(last.generation_tps, 1), "mlx_prompt_tps": round(last.prompt_tps, 1),
                    "peak_mem_gb": round(last.peak_memory, 2), "finish": last.finish_reason}})

send({"event": "bye", "paused_total_s": round(paused_total, 1), "peak_gpu_gb": round(mx.get_peak_memory() / 1e9, 2),
      "max_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2)})
conn.close(); srv.close()
os._exit(0)   # do not wait for the daemon reader thread / Metal teardown
