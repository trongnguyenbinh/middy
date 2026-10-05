// Self-check for main/mic_state.js (Lỗi 9b). Run: node tools/mic_state_check.js
const assert = require('assert')
const fs = require('fs'), os = require('os'), path = require('path')
const { classify, classifyOne, createMicStateWatcher } = require('../main/mic_state.js')
const { micInputEffective } = require('../main/meeting_detector.js')
let n = 0
const test = async (name, fn) => { await fn(); n++; console.log('PASS', name) }

;(async () => {
  await test('labels -> app mic state (a label says what the control WILL do)', () => {
    const cases = [
      ['Mute mic (⌘+Shift+M)', true], ['Unmute mic (⌘+Shift+M)', false],       // Teams (expected wording, to confirm live)
      ['Mute', true], ['Unmute', false], ['Mute Audio', true], ['Unmute Audio', false],   // Zoom toolbar / Meeting menu
      ['Turn off microphone', true], ['Turn on microphone', false],
      ['Tắt tiếng', true], ['Bật tiếng', false], ['Bỏ tắt tiếng', false], ['Tắt micrô', true], ['Bật micrô', false],
      ['Mute All', null], ['Mute notifications', null], ['Mute chat', null], ['Mute participants', null], ['Tắt tiếng tất cả', null],
      ['Microphone', null], ['Mic', null], ['Camera', null], ['', null], [undefined, null],
    ]
    for (const [label, want] of cases) assert.strictEqual(classifyOne(label), want, JSON.stringify(label))
  })
  await test('several controls: agreement wins, contradiction -> null (never a wrong "off")', () => {
    assert.deepStrictEqual(classify([{ label: 'Unmute' }, { label: 'Unmute Audio' }]).appMicOn, false)
    assert.deepStrictEqual(classify([{ label: 'Mute' }, { label: 'Mute All' }]).appMicOn, true)
    const c = classify([{ label: 'Mute' }, { label: 'Unmute Audio' }]); assert.strictEqual(c.appMicOn, null); assert.strictEqual(c.conflict, true)
    assert.strictEqual(classify([]).appMicOn, null); assert.strictEqual(classify(undefined).appMicOn, null)
  })
  await test('end to end with the 3 cases: app state x Middy button', () => {
    const eff = (label, button) => micInputEffective({ appMicOn: classify([{ label }]).appMicOn, button })
    assert.strictEqual(eff('Unmute mic', false), false)   // 1/ muted in Teams + Middy off -> nothing
    assert.strictEqual(eff('Unmute mic', true), true)     // 2/ muted in Teams + Middy on -> mic
    assert.strictEqual(eff('Mute mic', false), true)      // 3/ unmuted in Teams -> always mic
    assert.strictEqual(eff('Microphone', false), false)   // unreadable -> button decides
  })
  await test('watcher: parses helper lines, untrusted/not running -> null, dedups, reports exit', async () => {
    const fake = path.join(os.tmpdir(), 'midy_fake_mic_state_' + process.pid + '.sh')
    fs.writeFileSync(fake, `#!/bin/sh
echo '{"trusted":false,"running":true,"controls":[]}'
echo '{"trusted":true,"running":true,"controls":[{"role":"AXButton","label":"Unmute mic"}]}'
echo '{"trusted":true,"running":true,"controls":[{"role":"AXButton","label":"Unmute mic"}],"scan_ms":5}'
echo '{"trusted":true,"running":true,"controls":[{"role":"AXButton","label":"Mute mic"}]}'
echo 'not json'
echo '{"trusted":true,"running":false,"controls":[]}'
`, { mode: 0o700 })
    const seen = []
    await new Promise((res) => { createMicStateWatcher({ binary: fake, bundleId: 'x', onState: (s) => { seen.push(s); if (s.exited) res() } }) })
    fs.unlinkSync(fake)
    assert.deepStrictEqual(seen.map((s) => s.appMicOn), [null, false, true, null, null])
    assert.deepStrictEqual(seen.map((s) => s.trusted), [false, true, true, true, null])
    assert.strictEqual(seen[seen.length - 1].exited, true)
  })
  console.log(n + '/' + n + ' PASS')
})().catch((e) => { console.error('FAIL', e.message); process.exit(1) })
