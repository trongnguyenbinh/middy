"""F7 check: build the MoM from the SAME live notes with the old prompt (mom.md.truoc_F7_20260927) and the new one, using the
LLM worker directly. Prints numbers only (lengths, sections, term overlap), never the text.
  mom_compare.py <run-name>   (uses run/<run>/notes_parts.md)
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
run = os.path.join(HERE, "..", "run", sys.argv[1])
parts = open(os.path.join(run, "notes_parts.md")).read()
sock = os.path.join(HERE, "..", "run", "mom_cmp.sock")
if os.path.exists(sock):
    os.unlink(sock)
w = subprocess.Popen([sys.executable, os.path.join(HERE, "llm_worker.py"), "--socket", sock], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
for _ in range(600):
    if os.path.exists(sock):
        break
    time.sleep(0.1)
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(sock); r, f = c.makefile("r"), c.makefile("w")
ready = json.loads(r.readline()); print("ready", ready.get("load_s"))
out = {}
for tag, path in (("old", "mom.md.truoc_F7_20260927"), ("new", "mom.md")):
    p = open(os.path.join(HERE, "prompts", path)).read().replace("{{LANGUAGE}}", "Vietnamese").replace("{{PARTS}}", parts)
    f.write(json.dumps({"cmd": "generate", "id": tag, "messages": [{"role": "user", "content": p}], "max_tokens": 3000}) + "\n"); f.flush()
    while True:
        e = json.loads(r.readline())
        if e.get("event") == "done":
            out[tag] = e; break
f.write(json.dumps({"cmd": "quit"}) + "\n"); f.flush()
def words(t): return re.findall(r"[^\W_]+", t.lower())
res = {}
for tag, e in out.items():
    t = e["text"]
    res[tag] = {"gen_tokens": e["stats"]["gen_tokens"], "wall_s": e["stats"]["wall_s"], "finish": e["stats"]["finish"], "words": len(words(t)),
                "sections": re.findall(r"^## (.*)$", t, re.M), "action_rows": len(re.findall(r"^\|", t, re.M)), "q_marks": t.count("(?)"),
                "mentions_10_minutes": bool(re.search(r"10.?min|10 phút|part", t, re.I))}
a, b = set(words(out["old"]["text"])), set(words(out["new"]["text"]))
res["shared_vocab_jaccard"] = round(len(a & b) / max(1, len(a | b)), 3)
res["input_words"] = len(words(parts))
print(json.dumps(res, ensure_ascii=False))
