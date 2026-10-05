// The headless self-check scripts that already live in app/tools (node only: no Electron window, no audio device).
const test = require('node:test')
const assert = require('node:assert')
const { execFileSync } = require('child_process')
const path = require('path')

for (const f of ['select_check.mjs', 'shortcut_check.js', 'meeting_detector_check.js', 'mic_state_check.js']) {
  test('tools/' + f, () => {
    const out = execFileSync(process.execPath, [path.join(__dirname, '..', 'tools', f)], { encoding: 'utf8', timeout: 60000 })
    assert.match(out, /PASS/)
  })
}
