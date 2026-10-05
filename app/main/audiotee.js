// System audio via audiotee (upstream build pinned in M1: tools/audiotee-src, commit 56ac954, MIT), run from the main
// process: raw s16le 16 kHz mono, 250 ms chunks on stdout. Each chunk goes to two sinks:
// the daemon (stream 0) and the renderer (far-end reference for AEC3).
// --fake-system <wav> replaces audiotee by a 16 kHz wav played in real time (blind regression runs, nothing on the speaker).
const path = require('path')
const fs = require('fs')
const { spawn } = require('child_process')

// packaged app: main/ lives inside app.asar, so the workspace root (daemon, venv, run/, tools/) is given explicitly (rule A3: full path)
const ROOT = process.env.MIDY_ROOT || (require('electron').app.isPackaged ? path.join(require('os').homedir(), 'middy') : path.resolve(__dirname, '..', '..'))
const AUDIOTEE = path.join(ROOT, 'tools', 'audiotee-src', '.build', 'release', 'audiotee')
const SR = 16000, CHUNK_S = 0.25, BYTES = SR * CHUNK_S * 2

class SystemAudio {
  constructor(onChunk, log, fakeWav) {
    this.onChunk = onChunk; this.log = log || (() => {}); this.fakeWav = fakeWav
    this.proc = null; this.timer = null; this.chunks = 0
  }

  start() {
    if (this.fakeWav) return this._startFake()
    this.proc = spawn(AUDIOTEE, ['--sample-rate', String(SR), '--chunk-duration', String(CHUNK_S)], { stdio: ['ignore', 'pipe', 'pipe'] })
    let rest = Buffer.alloc(0)
    this.proc.stdout.on('data', (d) => {
      rest = Buffer.concat([rest, d])
      while (rest.length >= BYTES) { this._emit(rest.subarray(0, BYTES)); rest = rest.subarray(BYTES) }
    })
    this.proc.stderr.on('data', (d) => this.log('audiotee: ' + d.toString().trim().slice(0, 200)))
    this.proc.on('exit', (c) => this.log('audiotee exited ' + c))
    this.mode = 'audiotee'
  }

  _startFake() {
    const b = fs.readFileSync(this.fakeWav)
    // minimal RIFF parse: find the "data" chunk (16 kHz s16le mono expected)
    let off = 12, data = null
    while (off + 8 <= b.length) {
      const id = b.toString('ascii', off, off + 4), len = b.readUInt32LE(off + 4)
      if (id === 'data') { data = b.subarray(off + 8, off + 8 + len); break }
      off += 8 + len + (len & 1)
    }
    if (!data) throw new Error('no data chunk in ' + this.fakeWav)
    const t0 = Date.now(); let i = 0
    const tick = () => {
      const due = Math.floor((Date.now() - t0) / (CHUNK_S * 1000))
      while (i / BYTES < due && i + BYTES <= data.length) { this._emit(data.subarray(i, i + BYTES)); i += BYTES }
      if (i + BYTES > data.length) { clearInterval(this.timer); this.timer = null; this.log('fake system audio ended') }
    }
    this.timer = setInterval(tick, 50)
    this.mode = 'fake'
  }

  _emit(chunk) { this.chunks++; this.onChunk(Buffer.from(chunk)) }

  stop() {
    if (this.timer) { clearInterval(this.timer); this.timer = null }
    if (this.proc && this.proc.exitCode === null) this.proc.kill('SIGTERM')
    this.proc = null
  }
}

module.exports = { SystemAudio, AUDIOTEE }
