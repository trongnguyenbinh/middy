Middy {{VERSION}} for Apple Silicon Macs (macOS 14.2 or later). This build is set up for **Minutes by Claude Code**: Middy
records, transcribes and labels speakers on your Mac, and Claude Code writes the minutes through Middy's MCP server. No local
LLM is downloaded or loaded.

**Download size:** the app (~120 MB zip) and the runtime bundle are small; `install.sh` then downloads the models from their
official sources, about **4.4 GB** (Qwen3-ASR 1.7B) + 37 MB (Silero VAD, CAM++, pyannote segmentation), each checked against a
pinned SHA256, resumable. The local Gemma (8.9 GB) is optional: `install.sh --with-local-llm`.

**Memory:** about 8.5 GB while recording (measured), 16 GB of RAM recommended.

### Install
1. Download `Middy-{{VERSION}}-mac-arm64.zip`, `middy-runtime-{{VERSION}}.tar.gz` and `SHA256SUMS` into one folder, then
   `shasum -a 256 -c SHA256SUMS`.
2. `tar -xzf middy-runtime-{{VERSION}}.tar.gz && middy-runtime-{{VERSION}}/install.sh` (needs `uv` or `python3.12`;
   installs to `~/middy`, the models, the two Python venvs, and Middy.app into Applications).
3. Open Middy. The app is ad-hoc signed, not notarized: `install.sh` clears the download flag; if you install the app by
   hand, macOS blocks the first open: right-click Middy → Open (macOS 14), or System Settings → Privacy & Security →
   Open Anyway (macOS 15+), or `xattr -dr com.apple.quarantine /Applications/Middy.app`.
4. Allow Microphone and System Audio Recording at the first meeting.
5. Paste the `claude mcp add ...` line that install.sh printed.

Full notes: README.md.
