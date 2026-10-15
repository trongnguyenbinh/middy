// 15/10: a missing audiotee binary must not become an uncaught exception (Electron's error dialog froze the main process and no
// audio reached the daemon). start() throws a clear error instead; the caller records the mic only.
const test = require('node:test')
const assert = require('node:assert')
const os = require('os')
const fs = require('fs')
const path = require('path')

test('missing audiotee: start() throws a clear error, nothing is spawned', () => {
  process.env.MIDY_ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'mdy-'))           // a root without tools/audiotee-src
  const { SystemAudio } = require('../main/audiotee.js')
  const logs = []
  const s = new SystemAudio(() => {}, (m) => logs.push(m))
  assert.throws(() => s.start(), /audiotee not found at .*tools\/audiotee-src\/\.build\/release\/audiotee/)
  assert.strictEqual(s.proc, null)
})
