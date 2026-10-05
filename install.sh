#!/bin/bash
# Middy installer for a release bundle (Apple Silicon, macOS 14.2+). Safe to run again: every step checks what is already there.
#   ./install.sh [--home DIR] [--app Middy-<v>-mac-arm64.zip|Middy.app] [--models-from DIR] [--with-local-llm] [--no-app]
# 1. copies the runtime (daemon, MCP server, helpers) to --home (default ~/middy, where Middy.app looks for it)
# 2. creates the Python 3.12 venvs: .venv (daemon, models runtime) and .venv-mcp (MCP server for Claude Code), with uv if present
# 3. downloads the models from their official sources at pinned revisions, checks SHA256, resumes a cut download:
#    Qwen3-ASR 1.7B (~4.4 GB) + Silero VAD + CAM++ + pyannote segmentation (~37 MB). Gemma (8.9 GB) only with --with-local-llm.
#    --models-from DIR reuses files of an existing models/ directory (same layout) instead of downloading them.
# 4. installs Middy.app into /Applications (or ~/Applications) and removes the download quarantine flag
# 5. prints the `claude mcp add` line with this Mac's paths
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${MIDDY_HOME:-$HOME/middy}
APP_SRC="" ; MODELS_FROM="" ; WITH_LLM=0 ; NO_APP=0

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case $1 in
    --home) DEST=$2; shift ;;
    --app) APP_SRC=$2; shift ;;
    --models-from) MODELS_FROM=$2; shift ;;
    --with-local-llm) WITH_LLM=1 ;;
    --no-app) NO_APP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

# ---- 0. this Mac ------------------------------------------------------------------------------------------------------------
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "Middy needs an Apple Silicon Mac (arm64)"
OS=$(sw_vers -productVersion); IFS=. read -r MAJ MIN _ <<< "$OS.0"
{ [ "$MAJ" -gt 14 ] || { [ "$MAJ" -eq 14 ] && [ "${MIN:-0}" -ge 2 ]; }; } || die "macOS 14.2 or later is needed (this Mac: $OS)"
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
[ "$RAM_GB" -ge 16 ] || warn "this Mac has ${RAM_GB} GB of memory; Middy needs about 8.5 GB while recording (16 GB recommended)"
mkdir -p "$DEST"; DEST=$(cd "$DEST" && pwd)
say "Middy home: $DEST"

# ---- 1. runtime files -------------------------------------------------------------------------------------------------------
if [ "$SRC" != "$DEST" ]; then
  for d in proto claude_mcp templates tools; do
    [ -d "$SRC/$d" ] || die "$SRC/$d missing: run install.sh from the extracted middy-runtime folder"
    rsync -a --delete --exclude __pycache__ "$SRC/$d/" "$DEST/$d/"
  done
  for f in requirements.txt install.sh LICENSE README.md; do [ -f "$SRC/$f" ] && cp -p "$SRC/$f" "$DEST/"; done
fi
xattr -dr com.apple.quarantine "$DEST/tools" 2>/dev/null || true          # helpers built by the release workflow
mkdir -p "$DEST/run" "$DEST/models"; chmod 700 "$DEST/run"

# ---- 2. Python venvs --------------------------------------------------------------------------------------------------------
UV=$(command -v uv || true)
PY312=$(command -v python3.12 || true)
[ -n "$UV$PY312" ] || die "need uv (brew install uv) or python3.12 (brew install python@3.12)"
venv() {   # venv <dir> <requirements>: created once, packages installed again only when the requirements file changed
  local dir=$1 req=$2 stamp=$1/.middy-requirements
  if [ ! -x "$dir/bin/python" ]; then
    say "creating $(basename "$dir") (Python 3.12)"
    if [ -n "$UV" ]; then "$UV" venv -q --python 3.12 "$dir"; else "$PY312" -m venv "$dir"; fi
  fi
  if [ -f "$stamp" ] && cmp -s "$stamp" "$req"; then echo "   $(basename "$dir"): up to date"; return; fi
  say "installing $(basename "$req") into $(basename "$dir")"
  if [ -n "$UV" ]; then "$UV" pip install -q --python "$dir/bin/python" -r "$req"; else "$dir/bin/pip" install -q -r "$req"; fi
  cp "$req" "$stamp"
}
venv "$DEST/.venv" "$DEST/requirements.txt"
venv "$DEST/.venv-mcp" "$DEST/claude_mcp/requirements.txt"

# ---- 3. models --------------------------------------------------------------------------------------------------------------
QWEN_REV=7278e1e70fe206f11671096ffdd38061171dd6e5
GEMMA_REV=45c20664205a7d600425ea23efed8ba8b8543fc3
QWEN_DIR=hf/hub/models--Qwen--Qwen3-ASR-1.7B
HF=https://huggingface.co
GH=https://github.com/k2-fsa/sherpa-onnx/releases/download
# path under models/ | size | sha256 | official URL
MANIFEST="
$QWEN_DIR/snapshots/$QWEN_REV/chat_template.json 1161 75a8cfca24f00de72d796fbfed6858fc9614ef3dabd8696684cc3bc03a9c58ff $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/chat_template.json
$QWEN_DIR/snapshots/$QWEN_REV/config.json 6194 2e74a751548b8ad7d7526d29365ad8144c345d8b412b1152d25dc6698452712f $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/config.json
$QWEN_DIR/snapshots/$QWEN_REV/generation_config.json 142 1da527824d81e07118facff437e03f2e24a23311e3bdeb2368973fe77e5f275c $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/generation_config.json
$QWEN_DIR/snapshots/$QWEN_REV/merges.txt 1671853 8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5 $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/merges.txt
$QWEN_DIR/snapshots/$QWEN_REV/model-00001-of-00002.safetensors 4220320824 a4cd1f1a04d90b757dc7f7dd26254e69a013b19e80efe590a83c6a3bde8608d6 $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/model-00001-of-00002.safetensors
$QWEN_DIR/snapshots/$QWEN_REV/model-00002-of-00002.safetensors 478200688 6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/model-00002-of-00002.safetensors
$QWEN_DIR/snapshots/$QWEN_REV/model.safetensors.index.json 64821 f994739fe38e5210b9e3e8ce6c6307315e2ceac3cb630e7b7414d69dce520f60 $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/model.safetensors.index.json
$QWEN_DIR/snapshots/$QWEN_REV/preprocessor_config.json 330 45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/preprocessor_config.json
$QWEN_DIR/snapshots/$QWEN_REV/tokenizer_config.json 12487 4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/tokenizer_config.json
$QWEN_DIR/snapshots/$QWEN_REV/vocab.json 2776833 ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910 $HF/Qwen/Qwen3-ASR-1.7B/resolve/$QWEN_REV/vocab.json
silero_vad_v6.onnx 1245151 4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2 https://github.com/SYSTRAN/faster-whisper/raw/v1.2.1/faster_whisper/assets/silero_vad_v6.onnx
3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx 29596978 357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b $GH/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx
sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 6958444 24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488 $GH/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
"
GEMMA_MANIFEST="
gemma-4-E4B-it-MLX-8bit/chat_template.jinja 16317 781d10940fbc44be40064b5d43a056fc486c84ceaa55538226368b57314132bf $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/chat_template.jinja
gemma-4-E4B-it-MLX-8bit/config.json 36493 abb24d99b7ef2ce7a2b721f78a32e6e692795fcd0d75462d72794260bf1208e9 $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/config.json
gemma-4-E4B-it-MLX-8bit/generation_config.json 208 d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/generation_config.json
gemma-4-E4B-it-MLX-8bit/model-00001-of-00002.safetensors 4964690710 7c557043aeb5608ca838d3d1215182b63310ed917d71faac07e9d6f81d183805 $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/model-00001-of-00002.safetensors
gemma-4-E4B-it-MLX-8bit/model-00002-of-00002.safetensors 3974803524 c959486fb4944bdd7da6903f69d9650548d867e03bf507cae95cee87e75b0e22 $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/model-00002-of-00002.safetensors
gemma-4-E4B-it-MLX-8bit/model.safetensors.index.json 295363 f935769f2e36866177f480352e8336e1be83f89273fefb04b18392c3a1d5bc82 $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/model.safetensors.index.json
gemma-4-E4B-it-MLX-8bit/processor_config.json 902 1bd0d00776284f369c1eff5fb631e865dfcdca861e0b7d60dbef27fcf37436a8 $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/processor_config.json
gemma-4-E4B-it-MLX-8bit/tokenizer.json 32169626 cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/tokenizer.json
gemma-4-E4B-it-MLX-8bit/tokenizer_config.json 21700 16f4a5a617a11af24b4bf474109800989e7d2b5b1ca22f73a561ec2398308e8d $HF/lmstudio-community/gemma-4-E4B-it-MLX-8bit/resolve/$GEMMA_REV/tokenizer_config.json
"
[ "$WITH_LLM" = 1 ] && MANIFEST="$MANIFEST$GEMMA_MANIFEST"
M="$DEST/models"

ok() {   # ok <file> <size> <sha>: right size and SHA256 (remembered in <file>.sha256 so a rerun does not hash 4 GB again)
  local f=$1 size=$2 sha=$3
  [ -f "$f" ] && [ "$(stat -f %z "$f")" = "$size" ] || return 1
  [ -f "$f.sha256" ] && [ "$(cat "$f.sha256")" = "$sha" ] && [ "$f.sha256" -nt "$f" ] && return 0
  [ "$(shasum -a 256 "$f" | cut -d' ' -f1)" = "$sha" ] || return 1
  echo "$sha" > "$f.sha256"
}

need=0 todo=""
while read -r rel size sha url; do
  [ -n "$rel" ] || continue
  f="$M/$rel"
  if ok "$f" "$size" "$sha"; then continue; fi
  if [ -n "$MODELS_FROM" ] && [ -f "$MODELS_FROM/$rel" ]; then        # reuse (APFS clone: no extra space when possible)
    mkdir -p "$(dirname "$f")"; src=$(realpath "$MODELS_FROM/$rel")           # Hugging Face caches hold symlinks to blobs
    cp -c "$src" "$f" 2>/dev/null || cp "$src" "$f"
    if ok "$f" "$size" "$sha"; then echo "   reused $rel"; continue; fi
    warn "$MODELS_FROM/$rel does not match the expected SHA256; downloading it"; rm -f "$f"
  fi
  have=0; [ -f "$f.part" ] && have=$(stat -f %z "$f.part")
  need=$(( need + size - have )); todo="$todo$rel $size $sha $url
"
done <<< "$MANIFEST"

if [ -n "$todo" ]; then
  free=$(( $(df -k "$M" | awk 'NR==2 {print $4}') * 1024 ))
  gb() { awk -v b="$1" 'BEGIN { if (b >= 1e9) printf "%.1f GB", b / 1e9; else printf "%.0f MB", b / 1e6 }'; }
  say "models to download: $(gb $need) (free on this disk: $(gb $free))"
  [ "$free" -gt $(( need + 1000000000 )) ] || die "not enough free space: $(gb $need) + 1 GB margin needed"
  while read -r rel size sha url; do
    [ -n "$rel" ] || continue
    f="$M/$rel"; mkdir -p "$(dirname "$f")"
    echo "   $rel"
    for try in 1 2 3 4 5; do                                          # -C -: resume the .part file after a cut
      if curl -fL --retry 3 --retry-delay 2 -C - --progress-bar -o "$f.part" "$url"; then break; fi
      [ "$try" = 5 ] && die "download failed: $url (run install.sh again to resume)"
      warn "download interrupted, resuming ($try)"; sleep 2
    done
    mv "$f.part" "$f"
    ok "$f" "$size" "$sha" || { rm -f "$f"; die "SHA256 mismatch for $rel (file removed; run install.sh again)"; }
  done <<< "$todo"
fi
mkdir -p "$M/$QWEN_DIR/refs"; echo -n "$QWEN_REV" > "$M/$QWEN_DIR/refs/main"   # offline Hugging Face cache: main -> pinned snapshot
PY_ONNX=sherpa-onnx-pyannote-segmentation-3-0/model.onnx
if ! ok "$M/$PY_ONNX" 5992913 220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079 2>/dev/null; then
  tar -xjf "$M/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" -C "$M" sherpa-onnx-pyannote-segmentation-3-0/model.onnx
  ok "$M/$PY_ONNX" 5992913 220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079 || die "pyannote model.onnx: SHA256 mismatch"
fi
say "models ready ($(du -sh "$M" | cut -f1)$( [ "$WITH_LLM" = 1 ] && echo ', with Gemma' ))"

# ---- 4. the app -------------------------------------------------------------------------------------------------------------
if [ "$NO_APP" = 0 ]; then
  [ -n "$APP_SRC" ] || APP_SRC=$(ls -d "$SRC"/../Middy-*-mac-arm64.zip "$SRC"/Middy-*-mac-arm64.zip "$SRC"/../Middy.app 2>/dev/null | head -1 || true)
  if [ -z "$APP_SRC" ]; then
    warn "Middy.app not found next to install.sh: pass --app <Middy-...-mac-arm64.zip> (or install the app yourself)"
  else
    APPS=/Applications; [ -w "$APPS" ] || { APPS=$HOME/Applications; mkdir -p "$APPS"; }
    TMP=$(mktemp -d)
    case $APP_SRC in
      *.zip) ditto -x -k "$APP_SRC" "$TMP" ;;
      *.app) ditto "$APP_SRC" "$TMP/Middy.app" ;;
      *) die "--app wants a .zip or a .app" ;;
    esac
    [ -d "$TMP/Middy.app" ] || die "no Middy.app in $APP_SRC"
    rm -rf "$APPS/Middy.app"; ditto "$TMP/Middy.app" "$APPS/Middy.app"; rm -rf "$TMP"
    # ad-hoc signed, not notarized (no paid Apple developer account): without this, Gatekeeper blocks the first open
    xattr -dr com.apple.quarantine "$APPS/Middy.app" 2>/dev/null || true
    say "installed $APPS/Middy.app"
  fi
fi

# ---- 5. done ----------------------------------------------------------------------------------------------------------------
[ "$DEST" = "$HOME/middy" ] || warn "Middy.app looks for its files in ~/middy. Link it once: ln -s \"$DEST\" ~/middy"
cat <<EOF

Middy is installed. Next:
  1. Open Middy (Applications). macOS asks for Microphone and System Audio Recording on the first meeting.
  2. Claude Code: register the MCP server once (copy the line):

     claude mcp add --scope user middy -- "$DEST/.venv-mcp/bin/python" "$DEST/claude_mcp/mcp_server.py"

  The release defaults to Settings -> "Minutes by Claude Code" (no local LLM). Run install.sh again any time: it only adds
  what is missing.
EOF
