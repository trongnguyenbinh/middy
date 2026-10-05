"""Per-process CPU energy / CPU time / GPU time and machine GPU utilisation, without sudo (Việc 19/20).
CPU energy: proc_pid_rusage(RUSAGE_INFO_V6).ri_energy_nj (own-user processes). GPU time: ioreg AGXDeviceUserClient AppUsage
accumulatedGPUTime per pid. `top -stats power` is not used: on this Mac its POWER column equals %CPU and leaves the GPU out.
"""
import ctypes
import re
import subprocess

_lib = ctypes.CDLL("/usr/lib/libproc.dylib")


def cputime(t):
    s = 0.0
    for p in re.split("[:-]", t):
        s = s * 60 + float(p)
    return s


def cpu_energy_j(pid):
    buf = ctypes.create_string_buffer(16 + 8 * 70)
    if _lib.proc_pid_rusage(int(pid), 6, buf) != 0:
        return None
    return int.from_bytes(buf.raw[16 + 8 * 40:24 + 8 * 40], "little") / 1e9


def gpu_seconds():
    txt = subprocess.run(["ioreg", "-l", "-w0", "-r", "-c", "AGXDeviceUserClient"], capture_output=True, text=True).stdout
    out, cur = {}, None
    for line in txt.splitlines():
        m = re.search(r'"IOUserClientCreator" = "pid (\d+),', line)
        if m:
            cur = m.group(1)
        elif '"AppUsage"' in line and cur:
            out[cur] = out.get(cur, 0) + sum(int(v) for v in re.findall(r'"accumulatedGPUTime"=(\d+)', line)) / 1e9
    return out


def gpu_util():
    m = re.findall(r'"Device Utilization %"=(\d+)', subprocess.run(["ioreg", "-r", "-c", "IOAccelerator", "-d", "1"], capture_output=True, text=True).stdout)
    return int(m[0]) if m else None


def processes():
    """[(pid, ppid, cpu_s, rss_gb, command)] of every process."""
    rows = []
    for line in subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,rss=,command="], capture_output=True, text=True).stdout.splitlines():
        p = line.split(None, 4)
        if len(p) == 5:
            rows.append((p[0], p[1], cputime(p[2]), int(p[3]) / 1e6, p[4]))
    return rows
