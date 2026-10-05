#!/bin/zsh
# Package post-step (no code re-signing!): electron-builder's output is linker-signed ad-hoc; re-signing it with hardened runtime
# + entitlements makes Chromium's GPU / network helpers exit 5 under the kernel sandbox-exec profile (measured 27/09, tests a-e).
# Anh chose not to distribute (27/09 21:56), so no hardened runtime / notarization is needed. This step only installs the launcher:
# the bundle's main executable `Middy` becomes a small Mach-O (tools/launcher.swift) that execs the real Electron binary
# `Middy-bin` under sandbox-exec (proto/nonet.sb), whatever way the app was opened (Finder / Dock / Spotlight / open / terminal).
# The App Sandbox entitlement files in build/ are kept for the day a Team ID exists.
set -e
APP=${1:-release/mac-arm64/Middy.app}
HERE=$(cd "$(dirname "$0")/.." && pwd)
if [ ! -f "$APP/Contents/MacOS/Middy-bin" ]; then mv "$APP/Contents/MacOS/Middy" "$APP/Contents/MacOS/Middy-bin"; fi
swiftc -O -o "$APP/Contents/MacOS/Middy" "$HERE/tools/launcher.swift"
file "$APP/Contents/MacOS/Middy" | grep -q Mach-O && echo "launcher installed"
# Lỗi 9b: macOS privacy grants (Microphone, System Audio Recording, Accessibility) are tied to the LAUNCHER's cdhash (TCC.db csreq
# `cdhash H"..."`, measured 28/09). Same launcher.swift + same compiler => same cdhash => grants survive rebuilds. A changed
# launcher => anh must allow all three again, so say it loudly. build/launcher.cdhash holds the hash anh has granted.
H=$(codesign -dv --verbose=4 "$APP/Contents/MacOS/Middy" 2>&1 | sed -n 's/^CDHash=//p')
REF="$HERE/build/launcher.cdhash"
if [ ! -f "$REF" ]; then echo "$H" > "$REF"; echo "launcher cdhash $H (recorded)"
elif [ "$(cat "$REF")" = "$H" ]; then echo "launcher cdhash $H (unchanged: macOS permissions are kept)"
else echo "WARNING launcher cdhash CHANGED $(cat "$REF") -> $H: macOS will ask Microphone, System Audio Recording and Accessibility AGAIN"; fi
codesign -dv "$APP/Contents/MacOS/Middy-bin" 2>&1 | grep -E "^Identifier|flags" | head -2
