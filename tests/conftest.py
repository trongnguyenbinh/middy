"""Unit tests for the Python daemon: pure logic only. No model, no MLX, no audio device, no network; runs on Linux CI.
Modules that import a model runtime at the top (onnxruntime, sherpa_onnx) get an empty stand-in when it is not installed:
the tests only reach their pure parts (VAD state machine, centroid matching)."""
import os
import sys
import types

PROTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proto")
sys.path.insert(0, PROTO)

for name in ("onnxruntime", "sherpa_onnx"):
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = types.ModuleType(name)
