// Self-check for main/shortcut.js (Việc 16), headless: a fake globalShortcut and clock; no real key is pressed.
// Run: node tools/shortcut_check.js
const assert = require('assert')
const { DEFAULT_SHORTCUT, accelFromKey, label, createShortcut } = require('../main/shortcut.js')

let pass = 0
const t = (name, fn) => { fn(); pass++; console.log('PASS', name) }

function rig(taken = []) {
  const reg = new Map(), calls = []
  const gs = { register: (a, f) => { if (taken.includes(a) || reg.has(a)) return false; reg.set(a, f); return true }, unregister: (a) => reg.delete(a) }
  let meeting = false, clock = 0
  const sc = createShortcut({ globalShortcut: gs, isMeeting: () => meeting, start: () => { calls.push('start'); meeting = true }, stop: () => { calls.push('stop'); meeting = false }, now: () => clock })
  return { sc, reg, calls, tick: (ms) => { clock += ms }, press: (a) => reg.get(a)() }
}

t('default ⌃⌥R registers and shows as ⌃⌥R', () => {
  const r = rig(); const s = r.sc.register(DEFAULT_SHORTCUT)
  assert.deepStrictEqual([s.ok, s.accelerator, r.sc.state().label], [true, 'Control+Alt+R', '⌃⌥R'])
})
t('press: idle -> start, in meeting -> stop (same functions as Record / End meeting)', () => {
  const r = rig(); r.sc.register(DEFAULT_SHORTCUT)
  r.tick(5000); r.press(DEFAULT_SHORTCUT); r.tick(5000); r.press(DEFAULT_SHORTCUT)
  assert.deepStrictEqual(r.calls, ['start', 'stop'])
})
t('double press within 1 s is ignored', () => {
  const r = rig(); r.sc.register(DEFAULT_SHORTCUT)
  r.tick(5000); r.press(DEFAULT_SHORTCUT); r.tick(300); assert.strictEqual(r.sc.onPress(), 'ignored'); r.tick(600); assert.strictEqual(r.sc.onPress(), 'ignored')
  r.tick(200); r.press(DEFAULT_SHORTCUT)                 // 1.1 s after the accepted press: a real second press
  assert.deepStrictEqual(r.calls, ['start', 'stop'])
})
t('taken by another app: error reported, previous shortcut keeps working', () => {
  const r = rig(['Command+Shift+S']); r.sc.register(DEFAULT_SHORTCUT)
  const s = r.sc.register('Command+Shift+S')
  assert.strictEqual(s.ok, true); assert.strictEqual(s.accelerator, 'Control+Alt+R'); assert.strictEqual(s.rejected, 'Command+Shift+S')
  assert.match(s.error, /already used/); assert.ok(r.reg.has('Control+Alt+R') && !r.reg.has('Command+Shift+S'))
})
t('taken at launch (nothing before): state says not registered, never silent', () => {
  const r = rig(['Control+Alt+R']); const s = r.sc.register(DEFAULT_SHORTCUT)
  assert.strictEqual(s.ok, false); assert.match(s.error, /⌃⌥R is already used/)
})
t('change: the old one is released, the new one works', () => {
  const r = rig(); r.sc.register(DEFAULT_SHORTCUT); r.sc.register('Command+Alt+M')
  assert.ok(!r.reg.has('Control+Alt+R') && r.reg.has('Command+Alt+M')); r.tick(5000); r.press('Command+Alt+M'); assert.deepStrictEqual(r.calls, ['start'])
})
t('recorder: physical key + modifiers -> accelerator; ⌥R (types "®") still gives R', () => {
  assert.deepStrictEqual(accelFromKey({ code: 'KeyR', key: '®', ctrlKey: true, altKey: true }), { accelerator: 'Control+Alt+R' })
  assert.deepStrictEqual(accelFromKey({ code: 'Digit5', metaKey: true, shiftKey: true }), { accelerator: 'Command+Shift+5' })
  assert.deepStrictEqual(accelFromKey({ code: 'F9' }), { accelerator: 'F9' })
  assert.ok(accelFromKey({ code: 'KeyR' }).error, 'a bare letter would catch normal typing')
  assert.ok(accelFromKey({ code: 'KeyR', shiftKey: true }).error)
  assert.ok(accelFromKey({ code: 'ControlLeft', ctrlKey: true }).error, 'modifier alone is not a shortcut')
  assert.strictEqual(label('Command+Shift+5'), '⌘⇧5')
})
console.log(`${pass}/7 PASS`)
