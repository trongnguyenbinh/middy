// IPC whitelist (preload/channels.js): every channel / daemon command the renderer really uses is allowed, nothing else is.
const test = require('node:test')
const assert = require('node:assert')
const fs = require('fs')
const path = require('path')
const Module = require('module')
const { INVOKE, ON, DAEMON_CMDS, allowedDaemonRequest } = require('../preload/channels.js')

const APP = path.join(__dirname, '..')
const read = (dir) => fs.readdirSync(dir, { recursive: true }).filter((f) => /\.(js|jsx|mjs)$/.test(f)).map((f) => fs.readFileSync(path.join(dir, f), 'utf8')).join('\n')
const renderer = read(path.join(APP, 'renderer'))
const main = read(path.join(APP, 'main'))
const found = (src, rx) => new Set([...src.matchAll(rx)].map((m) => m[1]))

test('every channel the renderer invokes is whitelisted and handled by main', () => {
  const used = found(renderer, /\.invoke\(\s*'([^']+)'/g)
  const handled = found(main, /ipcMain\.handle\('([^']+)'/g)
  assert.ok(used.size > 30, 'scan found the renderer channels')
  for (const c of used) { assert.ok(INVOKE.has(c), 'not whitelisted: ' + c); assert.ok(handled.has(c), 'no handler in main: ' + c) }
  for (const c of INVOKE) assert.ok(used.has(c), 'whitelisted but unused by the renderer: ' + c)
})

test('every event channel the renderer listens to is whitelisted and sent by main', () => {
  const used = found(renderer, /\.on\(\s*'([^']+)'/g)
  const sent = found(main, /(?:webContents\.send|broadcast)\('([^']+)'/g)
  for (const c of used) { assert.ok(ON.has(c), 'not whitelisted: ' + c); assert.ok(sent.has(c), 'never sent by main: ' + c) }
})

test('daemon commands: the UI set only', () => {
  const used = found(renderer, /invoke\('daemon',\s*\{\s*cmd:\s*'([^']+)'/g)
  assert.deepStrictEqual([...used].sort(), [...DAEMON_CMDS].sort())
  for (const cmd of used) assert.ok(allowedDaemonRequest({ cmd }))
  for (const cmd of ['export_docx', 'quit', 'start', 'stop', 'audio', 'glossary_add', 'subscribe', undefined]) assert.ok(!allowedDaemonRequest({ cmd }), cmd)
  for (const bad of [null, undefined, 'meetings', ['meetings'], 42]) assert.ok(!allowedDaemonRequest(bad))
})

test('preload exposes a bridge that refuses unknown channels', async () => {
  const calls = []
  const fake = {
    contextBridge: { exposeInMainWorld: (k, v) => { fake.exposed = v } },
    ipcRenderer: {
      invoke: (c, p) => { calls.push(['invoke', c, p]); return Promise.resolve('ok') },
      on: (c) => calls.push(['on', c]), removeListener: (c) => calls.push(['off', c]), send: (c) => calls.push(['send', c]),
    },
  }
  const load = Module._load
  Module._load = function (req, ...rest) { return req === 'electron' ? fake : load.call(this, req, ...rest) }
  global.location = { hash: '#library' }
  try { require('../preload/index.js') } finally { Module._load = load; delete global.location }
  const api = fake.exposed
  assert.strictEqual(api.window, 'library')
  assert.strictEqual(await api.invoke('meeting:state'), 'ok')
  await assert.rejects(api.invoke('shot', 'x'), /not allowed/)
  await assert.rejects(api.invoke('evil'), /not allowed/)
  assert.throws(() => api.on('evil', () => {}), /not allowed/)
  const off = api.on('meeting', () => {}); off()
  api.sendMic(new ArrayBuffer(4))
  assert.deepStrictEqual(calls.map((c) => c.slice(0, 2)), [['invoke', 'meeting:state'], ['on', 'meeting'], ['off', 'meeting'], ['send', 'mic-audio']])
})
