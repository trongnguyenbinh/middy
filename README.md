# Middy

[![CI](https://github.com/trongnguyenbinh/middy/actions/workflows/ci.yml/badge.svg)](https://github.com/trongnguyenbinh/middy/actions/workflows/ci.yml)

Local meeting notes for macOS. Middy records your meeting (microphone + the audio of the meeting app), transcribes it,
labels speakers, writes live notes and a Minutes of Meeting, and answers questions about the meeting, **all on your Mac**.
No cloud service is used: the app runs inside a macOS sandbox profile with network sockets blocked (`proto/nonet.sb`),
and Record stays disabled until a self-test confirms the network is blocked.

- Speech-to-text: Qwen3-ASR 1.7B (MLX), streaming + a refining second pass, Silero VAD
- Speakers: pyannote segmentation 3.0 + 3D-Speaker CAM++ embeddings (sherpa-onnx)
- Notes, MoM, "Ask anything": Gemma 4 E4B, 8-bit MLX (mlx-lm)
- Desktop shell: Electron (toolbar, meeting overlay, library); backend: a Python daemon over a Unix socket
- Export: Markdown, and a Word MoM filled into `templates/mom_template.docx` (use your own template with `MIDY_MOM_TEMPLATE`)

> Status: early, source-only. There is no prebuilt app; run it from source as below.

## Requirements

- Apple Silicon Mac, macOS 14.2 or later (system audio via Core Audio taps, through [audiotee](https://github.com/makeusabrew/audiotee), MIT)
- Python 3.12, Node.js with npm, Xcode command line tools (`swiftc`, `swift`)
- About 13 GB of disk for the models (Gemma 8.4 GB, Qwen3-ASR 4.4 GB)

## Install

The packaged-app paths assume the checkout lives at `~/middy`; set `MIDY_ROOT` to use another place.

```sh
git clone https://github.com/harleyb283/middy ~/middy && cd ~/middy

# Python backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Swift helpers
swiftc -O -o tools/audiocap tools/audiocap.swift -framework CoreAudio -framework AVFoundation
swiftc -O -o tools/ocr tools/ocr.swift
git clone https://github.com/makeusabrew/audiotee tools/audiotee-src
(cd tools/audiotee-src && git checkout 56ac954 && swift build -c release)

# Electron app
cd app && npm install && npm run build && npm run probe
```

## Models

Models are **not** in this repository; download them yourself from the official sources below. None of them is gated
(no Hugging Face login or token needed). The app runs offline (`HF_HUB_OFFLINE=1`), so download everything **before**
the first start. It does not download anything on its own.

| Model | Source | Put it at |
|---|---|---|
| Qwen3-ASR 1.7B (Apache-2.0) | [huggingface.co/Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Hugging Face cache under `models/hf` (or `MIDY_HF_HOME`) |
| Gemma 4 E4B instruct, MLX 8-bit (Apache-2.0) | [huggingface.co/lmstudio-community/gemma-4-E4B-it-MLX-8bit](https://huggingface.co/lmstudio-community/gemma-4-E4B-it-MLX-8bit) | `models/gemma-4-E4B-it-MLX-8bit/` |
| pyannote segmentation 3.0 (ONNX, sherpa-onnx) | [k2-fsa/sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models) | `models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx` |
| 3D-Speaker CAM++ speaker embedding | [k2-fsa/sherpa-onnx releases](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models) | `models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx` |
| Silero VAD v6 (MIT, as shipped by faster-whisper 1.2.1) | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1/faster_whisper/assets) | `models/silero_vad_v6.onnx` |

```sh
cd ~/middy && mkdir -p models
HF_HOME=models/hf .venv/bin/hf download Qwen/Qwen3-ASR-1.7B
.venv/bin/hf download lmstudio-community/gemma-4-E4B-it-MLX-8bit --local-dir models/gemma-4-E4B-it-MLX-8bit
curl -L https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 | tar -xj -C models
curl -L -o models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx
curl -L -o models/silero_vad_v6.onnx https://github.com/SYSTRAN/faster-whisper/raw/v1.2.1/faster_whisper/assets/silero_vad_v6.onnx
```

Each model keeps its own license; check them on the pages above before use.

## Run

```sh
cd ~/middy/app && npx electron .
```

macOS asks for Microphone and System Audio Recording permission on the first meeting. Accessibility is optional (lets
Middy follow the mute state of your meeting app). Meetings, transcripts and notes are stored in `run/` (SQLite), never
uploaded. Global shortcut to start/stop: ⌃⌥R (changeable in Settings).

## Layout

```
app/      Electron shell: main process (windows, tray, audio, network guard), renderer (React), Swift helpers in app/tools
proto/    Python daemon midyd.py: ASR / LLM workers, diarization, notes, MoM, Word export, SQLite store; *_test.py checks
tools/    Swift capture/OCR helpers and measurement scripts
templates mom_template.docx (neutral Word MoM frame)
```

Code comments sometimes refer to internal issue numbers (Lỗi / Việc N) and are partly in Vietnamese.

## Checks

No model, no audio device and no network are needed for these (CI runs them on every push and pull request):

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check proto tools tests && .venv/bin/python -m pytest      # unit tests; -m model runs the proto/*_test.py acceptance scripts
cd app && npm ci && npm run lint && npm test && npm run build
```

## License

MIT. See [LICENSE](LICENSE). Third-party models and libraries keep their own licenses.
