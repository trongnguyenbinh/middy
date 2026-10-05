// Bundle the renderer with esbuild (devDependency, see package.json).
// Output: dist/renderer.js + dist/worklets/*.js (worklets are copied verbatim: AudioWorkletGlobalScope cannot import).
import { mkdirSync, copyFileSync, readdirSync, readFileSync, writeFileSync } from 'fs'
import { createRequire } from 'module'
const require = createRequire(import.meta.url)
const esbuild = require('esbuild')

mkdirSync('dist/worklets', { recursive: true })
await esbuild.build({
  entryPoints: ['renderer/index.jsx'],
  bundle: true,
  outfile: 'dist/renderer.js',
  format: 'iife',
  jsx: 'automatic',
  loader: { '.wasm': 'binary' },
  define: { 'process.env.NODE_ENV': '"production"' },
  logLevel: 'info',
})
for (const f of readdirSync('renderer/worklets')) copyFileSync('renderer/worklets/' + f, 'dist/worklets/' + f)
// AEC3 (BSD-3-Clause, the same library the reference app ships: webrtcaec3 0.3.0, taken from npm, not from the reference app's bundle) + our processor
// in ONE worklet module: AudioWorkletGlobalScope has no import/fetch. The glue variant chosen by AEC_GLUE (see package.json).
// The worklet scope has no fetch, so the wasm binary is embedded as base64 and handed to the Emscripten factory as
// `wasmBinary` (no file access at run time).
const glue = readFileSync('node_modules/@ennuicastr/webrtcaec3.js/dist/webrtcaec3-0.3.0.js', 'utf8')
const wasmB64 = readFileSync('node_modules/@ennuicastr/webrtcaec3.js/dist/webrtcaec3-0.3.0.wasm').toString('base64')
writeFileSync('dist/worklets/aec-bundle.js', 'var WebRtcAec3; var WebRtcAec3Wasm;\nvar __AEC_WASM_B64 = "' + wasmB64 + '";\ntry {\n' + glue + '\n} catch (e) { globalThis.__aecGlueError = e }\n;\n' + readFileSync('renderer/worklets/aec-processor.js', 'utf8'))
copyFileSync('renderer/index.html', 'dist/index.html')
copyFileSync('renderer/styles.css', 'dist/styles.css')
console.log('built')
