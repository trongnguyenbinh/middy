// main/daemon.js against a fake midyd on a temporary Unix socket: newline-delimited JSON, replies matched in order,
// pending requests failed when the connection drops, one-shot requests, the event stream split across packets.
const test = require('node:test')
const assert = require('node:assert')
const fs = require('fs')
const net = require('net')
const os = require('os')
const path = require('path')

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'midy-'))
fs.mkdirSync(path.join(root, 'run'))
process.env.MIDY_ROOT = root                         // daemon.js reads it at load: no Electron, no real daemon
const { Daemon, SOCK } = require('../main/daemon.js')

// fake daemon: echoes {"ok":true,"echo":<cmd>} per request line; "subscribe" streams 3 events in awkward chunks
const seen = []
const conns = new Set()
const server = net.createServer((s) => {
  conns.add(s); s.on('close', () => conns.delete(s))
  let buf = '', chain = Promise.resolve()
  s.on('data', (d) => {
    buf += d.toString()
    let i
    while ((i = buf.indexOf('\n')) >= 0) {
      const req = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); seen.push(req)
      if (req.cmd === 'subscribe') {
        const lines = [{ ok: true, subscribed: true }, { type: 'transcript', text: 'xin chào' }, { type: 'note', text: 'n' }, { type: 'done' }].map((o) => JSON.stringify(o) + '\n').join('')
        s.write(lines.slice(0, 17)); setTimeout(() => s.write(lines.slice(17)), 20)
      } else if (req.cmd !== 'audio') {                     // audio: no reply, like midyd. Replies stay in order (midyd is serial per connection)
        chain = chain.then(() => new Promise((r) => setTimeout(r, req.cmd === 'slow' ? 30 : 0))).then(() => { if (!s.destroyed) s.write(JSON.stringify({ ok: true, echo: req.cmd }) + '\n') })
      }
    }
  })
})

test.before(() => new Promise((r) => server.listen(SOCK, r)))
test.after(() => { for (const s of conns) s.destroy(); server.close(); fs.rmSync(root, { recursive: true, force: true }) })

test('request before start says not connected', async () => {
  assert.deepStrictEqual(await new Daemon().request({ cmd: 'status' }), { ok: false, error: 'daemon not connected' })
})

test('replies come back in request order', async () => {
  const d = new Daemon(); await d.start()
  const r = await Promise.all([d.request({ cmd: 'slow' }), d.request({ cmd: 'meetings' }), d.request({ cmd: 'status' })])
  assert.deepStrictEqual(r.map((x) => x.echo), ['slow', 'meetings', 'status'])
  d.cmd.destroy(); d.audio.destroy()
})

test('audio frames are base64 JSON lines with no reply; empty chunks are not sent', async () => {
  const d = new Daemon(); await d.start(); seen.length = 0
  d.sendAudio(1, Buffer.from([1, 0, 2, 0]), 12.5); d.sendAudio(0, Buffer.alloc(0), 1)
  await d.request({ cmd: 'status' })                      // ordered after the audio line reached the server
  await new Promise((r) => setTimeout(r, 20))
  const audio = seen.filter((x) => x.cmd === 'audio')
  assert.deepStrictEqual(audio, [{ cmd: 'audio', stream: 1, t: 12.5, pcm: Buffer.from([1, 0, 2, 0]).toString('base64') }])
  d.cmd.destroy(); d.audio.destroy()
})

test('pending requests fail when the daemon connection closes', async () => {
  const d = new Daemon(); await d.start()
  const p = d.request({ cmd: 'slow' })
  for (const s of conns) s.destroy()
  assert.deepStrictEqual(await p, { ok: false, error: 'daemon connection closed' })
  assert.strictEqual(d.cmd, null)
})

test('requestOnce uses its own connection', async () => {
  const d = new Daemon()
  assert.deepStrictEqual(await d.requestOnce({ cmd: 'export_docx' }), { ok: true, echo: 'export_docx' })
})

test('subscribe: events split across packets arrive whole, in order, until done', async () => {
  const d = new Daemon(); const evs = []
  let done
  const finished = new Promise((resolve) => { done = resolve })
  await d.subscribe((ev) => { evs.push(ev); if (ev.type === 'done') done() })
  await finished
  assert.deepStrictEqual(evs.map((e) => e.type), ['transcript', 'note', 'done'])
  assert.strictEqual(evs[0].text, 'xin chào')
})
