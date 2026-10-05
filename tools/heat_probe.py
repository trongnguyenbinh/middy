"""Việc 20: heat / CPU / GPU during a REAL meeting in Middy — numbers only (no transcript, no meeting content, no sudo).
  tools/heat_probe.py start <label>     background; one line every 5 s to run/heat/<label>_<time>.jsonl; stops by itself after
                                        18 min, or 3 min after the app logs a Stop ("stop -> …" in run/midy_app.log)
  tools/heat_probe.py stop              stop it now
  tools/heat_probe.py report [file]     summary of a run (default: the newest), also written next to it as .summary.json
Groups: gemma (llm_worker) · asr · daemon · diar · app_main / app_renderer / app_gpu / app_other (Middy.app) · teams · zoom.
Machine: GPU utilisation, CPU usage (top), SoC die temperatures (tools/thermal: IOHIDEventSystemClient, no sudo), thermal state.
GPU per process is NOT reported: ioreg AppUsage charges GPU time to the "responsible" app (measured 30/09: an MLX worker's
300-token generation showed up on Safari; the worker's own client had no AppUsage at all). The machine's GPU utilisation is
used instead, plus the meeting's Gemma work from its stats.json (numbers only: rounds, tokens, seconds).
"""
import json
import os
import signal
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.realpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from procstat import cpu_energy_j, gpu_seconds, gpu_util, processes  # noqa: E402

OUT = os.path.join(ROOT, "run", "heat"); PIDF = os.path.join(OUT, "heat_probe.pid")
APP_LOG = os.environ.get("HEAT_APP_LOG", os.path.join(ROOT, "run", "midy_app.log"))    # env overrides: tests only
EVERY_S, MAX_MIN, AFTER_STOP_S = 5, float(os.environ.get("HEAT_MAX_MIN", 18)), float(os.environ.get("HEAT_AFTER_STOP_S", 180))


def group(cmd):
    if "llm_worker.py" in cmd: return "gemma"
    if "asr_worker.py" in cmd: return "asr"
    if "diar_offline.py" in cmd: return "diar"
    if "midyd.py" in cmd: return "daemon"
    if "Middy.app/Contents" in cmd or "/midy/app/" in cmd:
        if "Helper (Renderer)" in cmd: return "app_renderer"
        if "Helper (GPU)" in cmd: return "app_gpu"
        if cmd.split()[0].endswith("Middy-bin"): return "app_main"
        return "app_other"                           # launcher, network/plugin helpers, meeting-probe, mic-state, audiotee
    if "Microsoft Teams" in cmd or "MSTeams" in cmd or "com.microsoft.teams2" in cmd: return "teams"
    if "zoom.us" in cmd: return "zoom"
    return None


def machine():
    top = subprocess.run(["top", "-l", "1", "-n", "0"], capture_output=True, text=True).stdout
    cpu = next((l for l in top.splitlines() if l.startswith("CPU usage")), "")
    idle = float(cpu.split("sys,")[1].split("%")[0]) if "sys," in cpu else None
    try:
        th = json.loads(subprocess.run([os.path.join(HERE, "thermal")], capture_output=True, text=True, timeout=5).stdout)
        dies = [v for k, v in th["sensors"].items() if k.startswith("PMU tdie")]
        temp = {"soc_mean_c": round(statistics.mean(dies), 1) if dies else None, "soc_max_c": max(dies) if dies else None,
                "battery_c": th["sensors"].get("gas gauge battery"), "thermal_state": th["thermal_state"]}
    except Exception as e:                           # never silent: say why the temperature is missing
        temp = {"error": f"thermal helper: {e}"}
    return {"gpu_util": gpu_util(), "cpu_busy_pct": round(100 - idle, 1) if idle is not None else None, "load1": os.getloadavg()[0], **temp}


def sample():
    g = gpu_seconds()
    procs = {}
    for pid, ppid, cpu_s, rss, cmd in processes():
        k = group(cmd)
        if k:
            procs[pid] = {"g": k, "cpu_s": cpu_s, "cpu_j": cpu_energy_j(pid) or 0.0, "gpu_s": g.get(pid, 0.0), "rss_gb": round(rss, 3)}
    return {"t": time.time(), "machine": machine(), "procs": procs}


def run(label):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{label}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    open(PIDF, "w").write(f"{os.getpid()} {path}")
    log0 = os.path.getsize(APP_LOG) if os.path.exists(APP_LOG) else 0
    t0, stop_at = time.time(), None
    with open(path, "a") as f:
        while time.time() - t0 < MAX_MIN * 60 and (stop_at is None or time.time() < stop_at):
            f.write(json.dumps(sample()) + "\n"); f.flush()
            if stop_at is None and os.path.exists(APP_LOG):     # the app's state log only ("stop -> …"), never meeting text
                with open(APP_LOG, errors="ignore") as lg:
                    lg.seek(log0); new = lg.read(); log0 = lg.tell()
                if "stop -> " in new:
                    stop_at = time.time() + AFTER_STOP_S
            time.sleep(EVERY_S)
    os.remove(PIDF)
    report(path)


def gemma_work(t_start):
    """The newest meeting finished during the run: its stats.json numbers (live-notes rounds, tokens, seconds). No text is read."""
    runs = [os.path.join(ROOT, "run", d, "stats.json") for d in os.listdir(os.path.join(ROOT, "run")) if d.startswith("meeting ")]
    runs = [f for f in runs if os.path.exists(f) and os.path.getmtime(f) > t_start]
    if not runs:
        return None
    st = json.load(open(max(runs, key=os.path.getmtime)))
    ls = st.get("live_summary", {}); per = ls.get("per_summary", [])
    return {"meeting_audio_min": round(st.get("audio_s", 0) / 60, 1), "live_rounds": ls.get("n"), "live_gen_tokens": sum(p.get("gen_tokens", 0) for p in per),
            "live_gen_seconds": round(sum(p.get("wall_s", 0) for p in per), 1), "blocks_not_summarised": ls.get("blocks_refined_not_summarised"),
            "asks": len(st.get("asks", []))}


def report(path=None):
    if not path:
        path = max((os.path.join(OUT, f) for f in os.listdir(OUT) if f.endswith(".jsonl")), key=os.path.getmtime)
    rows = [json.loads(l) for l in open(path)]
    mins = (rows[-1]["t"] - rows[0]["t"]) / 60
    first, last = {}, {}
    for r in rows:                                    # cumulative counters: first seen / max seen per pid (exited workers read 0)
        for pid, p in r["procs"].items():
            first.setdefault(pid, dict(p) if r is rows[0] else {**p, "cpu_s": 0.0, "cpu_j": 0.0, "gpu_s": 0.0})
            old = last.get(pid)
            last[pid] = p if not old else {**p, **{k: max(old[k], p[k]) for k in ("cpu_s", "cpu_j", "gpu_s")}}
    groups = {}
    for pid, p in last.items():
        g = groups.setdefault(p["g"], {"cpu_s": 0.0, "cpu_j": 0.0, "gpu_s": 0.0})
        for k in g:
            g[k] += max(0.0, p[k] - first[pid][k])
    for g in groups.values():
        g.update(cpu_w=round(g["cpu_j"] / (mins * 60), 2), gpu_s_per_min=round(g["gpu_s"] / mins, 2), cpu_pct_of_1_core=round(100 * g["cpu_s"] / (mins * 60), 1))
        for k in ("cpu_s", "cpu_j", "gpu_s"):
            g[k] = round(g[k], 1)
    m = [r["machine"] for r in rows]
    def stat(k):
        v = [x[k] for x in m if isinstance(x.get(k), (int, float))]
        return {"mean": round(statistics.mean(v), 1), "max": round(max(v), 1)} if v else None
    states = {}
    for x in m:
        states[x.get("thermal_state", "?")] = states.get(x.get("thermal_state", "?"), 0) + 1
    for g in groups.values():                         # see the module docstring: per-process GPU time is not trustworthy
        g.pop("gpu_s", None); g.pop("gpu_s_per_min", None)
    out = {"file": path, "minutes": round(mins, 1), "samples": len(rows), "groups": groups,
           "middy_total": {k: round(sum(g[k] for n, g in groups.items() if n not in ("teams", "zoom")), 1) for k in ("cpu_s", "cpu_j")},
           "meeting_gemma_work": gemma_work(rows[0]["t"]),
           "machine": {"gpu_util_pct": stat("gpu_util"), "cpu_busy_pct": stat("cpu_busy_pct"), "soc_temp_c": stat("soc_mean_c"), "soc_temp_max_c": stat("soc_max_c"),
                       "battery_c": stat("battery_c"), "thermal_state_samples": states}}
    json.dump(out, open(path.replace(".jsonl", ".summary.json"), "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "start":
        os.makedirs(OUT, exist_ok=True)
        if os.path.exists(PIDF):
            sys.exit("already running: " + open(PIDF).read())
        p = subprocess.Popen([sys.executable, __file__, "_run", sys.argv[2] if len(sys.argv) > 2 else "run"], start_new_session=True,
                             stdout=open(os.path.join(OUT, "heat_probe.log"), "a"), stderr=subprocess.STDOUT)
        print(f"started pid {p.pid}; writes to {OUT}; stops by itself after {MAX_MIN} min or {AFTER_STOP_S / 60:g} min after Stop")
    elif cmd == "_run":
        run(sys.argv[2])
    elif cmd == "stop":
        if not os.path.exists(PIDF):
            sys.exit("not running")
        pid, path = open(PIDF).read().split(" ", 1)
        os.kill(int(pid), signal.SIGTERM); os.remove(PIDF)
        report(path)
    else:
        report(sys.argv[2] if len(sys.argv) > 2 else None)
