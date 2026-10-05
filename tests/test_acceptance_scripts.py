"""The model-backed acceptance scripts in proto/ (*_test.py), kept exactly as they are. They need the downloaded models,
Apple Silicon (MLX) and public audio files, so they carry the `model` marker and are skipped by default:
    .venv/bin/python -m pytest -m model          # on a Mac set up per README"""
import glob
import os
import subprocess
import sys

import pytest

from conftest import PROTO

SCRIPTS = sorted(os.path.basename(p) for p in glob.glob(os.path.join(PROTO, "*_test.py")))


def test_scripts_are_listed():
    assert {"ask_test.py", "bulk_delete_test.py", "prewarm_fault_test.py", "stop_restart_test.py", "ui_feed_test.py"} <= set(SCRIPTS)


@pytest.mark.model
@pytest.mark.parametrize("script", SCRIPTS)
def test_acceptance_script(script):
    r = subprocess.run([sys.executable, os.path.join(PROTO, script)], capture_output=True, text=True, timeout=3600)
    assert r.returncode == 0, r.stderr[-2000:]
