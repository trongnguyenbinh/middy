// Client of the Midy daemon (proto/midyd.py): newline-delimited JSON over ONE Unix domain socket per purpose,
// never TCP. `request` uses a command connection (one reply per line, in order); `subscribe` opens a second
// connection that turns into an event stream; `audio` frames are fire-and-forget on a third connection.
const net = require('net')
const path = require('path')
const { spawn } = require('child_process')
const fs = require('fs')

// packaged app: main/ lives inside app.asar, so the workspace root (daemon, venv, run/, tools/) is given explicitly (rule A3: full path)
const ROOT = process.env.MIDY_ROOT || (require('electron').app.isPackaged ? path.join(require('os').homedir(), 'middy') : path.resolve(__dirname, '..', '..'))            // packaged app: the source checkout at ~/middy unless MIDY_ROOT says otherwise
const PY = path.join(ROOT, '.venv', 'bin', 'python')
const SOCK = path.join(ROOT, 'run', 'midy.sock')

class Daemon {
  constructor(log) {
    this.log = log || (() => {})
    this.proc = null
    this.cmd = null            // command connection
    this.audio = null          // audio connection
    this.pending = []          // reply callbacks for the command connection, in order
    this.buf = ''
  }

  // one start at a time: Middy starts the daemon at launch (Lỗi 15, models kept warm) while a click may ask for it too
  start() { return (this._starting ||= this._start().finally(() => { this._starting = null })) }

  async _start() {
    if (this.proc && this.proc.exitCode === null && this.cmd) return
    if (fs.existsSync(SOCK)) {                               // stale socket of a daemon that died without unlinking
      try { const s = await this._connect(); s.destroy() } catch { try { fs.unlinkSync(SOCK) } catch {} }
    }
    if (!fs.existsSync(SOCK)) {
      this.proc = spawn(PY, [path.join(ROOT, 'proto', 'midyd.py'), '--socket', SOCK], { stdio: ['ignore', 'pipe', 'pipe'] })
      this.proc.stdout.on('data', (d) => this.log('midyd: ' + d.toString().trim()))
      this.proc.stderr.on('data', (d) => this.log('midyd! ' + d.toString().trim()))
      this.proc.on('exit', (c) => { this.log('midyd exited ' + c); this.proc = null })
      for (let i = 0; i < 100 && !fs.existsSync(SOCK); i++) await new Promise((r) => setTimeout(r, 100))
    }
    this.cmd = await this._connect()
    this.cmd.on('data', (d) => {
      this.buf += d.toString()
      let i
      while ((i = this.buf.indexOf('\n')) >= 0) {
        const line = this.buf.slice(0, i); this.buf = this.buf.slice(i + 1)
        const cb = this.pending.shift()
        if (cb) { try { cb(JSON.parse(line)) } catch (e) { cb({ ok: false, error: String(e) }) } }
      }
    })
    this.cmd.on('close', () => { this.cmd = null; this.pending.splice(0).forEach((cb) => cb({ ok: false, error: 'daemon connection closed' })) })
    this.audio = await this._connect()
  }

  _connect() {
    return new Promise((resolve, reject) => {
      const s = net.createConnection(SOCK)
      s.once('connect', () => resolve(s)); s.once('error', reject)
    })
  }

  request(obj) {
    return new Promise((resolve) => {
      if (!this.cmd) return resolve({ ok: false, error: 'daemon not connected' })
      this.pending.push(resolve)
      this.cmd.write(JSON.stringify(obj) + '\n')
    })
  }

  // One request on its own connection (Việc 13: export_docx runs 20-40 s and must not hold up the command connection).
  async requestOnce(obj) {
    const s = await this._connect()
    return new Promise((resolve) => {
      let buf = ''
      s.on('data', (d) => { buf += d.toString(); const i = buf.indexOf('\n'); if (i >= 0) { s.end(); try { resolve(JSON.parse(buf.slice(0, i))) } catch (e) { resolve({ ok: false, error: String(e) }) } } })
      s.on('error', (e) => resolve({ ok: false, error: String(e) }))
      s.write(JSON.stringify(obj) + '\n')
    })
  }

  // s16le 16 kHz mono chunk (Buffer) for stream 0 = system, 1 = mic. Same JSON+base64 shape as the reference app's audio_chunk.
  sendAudio(stream, buf, tCapture) {
    if (!this.audio || !buf || !buf.length) return
    this.audio.write(JSON.stringify({ cmd: 'audio', stream, t: tCapture, pcm: buf.toString('base64') }) + '\n')
  }

  // Event stream of the running meeting; onEvent(ev) until {type:"done"}.
  async subscribe(onEvent) {
    const s = await this._connect()
    let buf = ''
    s.write(JSON.stringify({ cmd: 'subscribe' }) + '\n')
    s.on('data', (d) => {
      buf += d.toString()
      let i
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1)
        let ev
        try { ev = JSON.parse(line) } catch { continue }
        if (ev.subscribed !== undefined) continue
        onEvent(ev)
        if (ev.type === 'done') s.end()
      }
    })
    return () => s.destroy()
  }

  async quit() {
    if (this.cmd) { await this.request({ cmd: 'quit' }); this.cmd.destroy(); this.cmd = null }
    if (this.audio) { this.audio.destroy(); this.audio = null }
    if (this.proc) { const p = this.proc; setTimeout(() => { if (p.exitCode === null) p.kill() }, 5000) }
  }
}

module.exports = { Daemon, SOCK, ROOT }
