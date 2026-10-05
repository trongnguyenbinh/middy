# Middy app (Electron shell) — build notes

- `node build.mjs` (`npm run build`) bundles the renderer with **esbuild 0.28.1**, a devDependency installed by `npm ci` /
  `npm install`. The packaged app does not depend on it at run time.
- `npm run dist` = build + `electron-builder --mac --dir` → `release/mac-arm64/Middy.app` (unsigned: `identity: null`).
- `tools/sign.sh` signs ad-hoc with hardened runtime + `build/entitlements.mac.nosandbox.plist`. App Sandbox entitlements are kept in
  `build/entitlements.mac*.plist` for the day a Team ID exists (ad-hoc + App Sandbox crashes Chromium helpers, measured 27/09).
- Network: the app re-execs itself under `sandbox-exec -f proto/nonet.sb` (main/index.js top) whatever way it was opened, then runs a
  TCP + DNS self-test; Record is disabled until the self-test reports "blocked". `--no-wrap` skips the re-exec (diagnostics only).
- Blind test flags: `--fake-mic <wav 48 kHz> --fake-system <wav 16 kHz> --auto-start --duration N --shots <dir> --force-aec |
  --force-headphone --no-llm-test --test-kill-mic N --keep-toolbar --language X`. Start it from source with `npx electron .`
  (README "Run"): the app wraps itself in `sandbox-exec`. The `run/midy.sh` / `run/midy_dev.sh` launchers mentioned in older notes
  were local scripts and are not in this repository (`run/` is gitignored).
- Checks: `npm run lint` (ESLint), `npm test` (node:test, no Electron window, no audio device), `npm run build`. CI runs them on
  every push / PR (`.github/workflows/ci.yml`).

## Name
- **User-visible name: "Middy"** (anh 27/09 22:42): productName / `Middy.app`, bundle display name, window titles, tray menu,
  TCC usage strings, README. **Internal names stay** to keep every path working: workspace `50_workspace/midy`, daemon
  `midyd.py`, socket `run/midy.sock`, bundle id `local.midy.app`, the `MIDY_*` env flags and the logo letter M.
