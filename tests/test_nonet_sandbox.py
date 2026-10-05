"""The privacy guard itself (proto/nonet.sb, macOS only): under the profile TCP and DNS fail, a Unix socket inside RUN works,
one outside RUN is refused. Runs on the macOS CI job; skipped elsewhere."""
import os
import shutil
import subprocess
import sys

import pytest

from conftest import PROTO

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"), reason="macOS sandbox-exec only")

PROBE = r"""
import os, socket, sys
run, other = sys.argv[1], sys.argv[2]
def tcp():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close(); return "OPEN"
    except OSError as e:
        return "blocked"
def dns():
    try:
        socket.getaddrinfo("apple.com", 443); return "RESOLVED"
    except OSError:
        return "blocked"
def unix(d):
    p = os.path.join(d, "t.sock")
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(p); s.listen(1); c = socket.socket(socket.AF_UNIX); c.connect(p); c.close(); return "ok"
    except OSError:
        return "blocked"
    finally:
        s.close()
print(tcp(), dns(), unix(run), unix(other))
"""


def test_profile_blocks_network_but_not_the_run_dir():
    import pathlib
    import tempfile
    base = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))          # short path: AF_UNIX paths are capped at 104 bytes on macOS
    run, other = base / "run", base / "other"
    run.mkdir(); other.mkdir()
    try:
        r = subprocess.run(["/usr/bin/sandbox-exec", "-f", os.path.join(PROTO, "nonet.sb"), "-D", f"RUN={os.path.realpath(run)}",
                            sys.executable, "-c", PROBE, os.path.realpath(run), os.path.realpath(other)], capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["blocked", "blocked", "ok", "blocked"]
