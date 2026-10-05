// Self-check for renderer/lib/select.mjs (Việc 18). Run: node tools/select_check.mjs
import assert from 'node:assert'
import { nextSelection, confirmText, resultText } from '../renderer/lib/select.mjs'

let pass = 0
const t = (name, fn) => { fn(); pass++; console.log('PASS', name) }
const order = [10, 9, 8, 7, 6]            // as listed (newest first)
const ids = (r) => [...r.picked].sort((a, b) => b - a)

t('click toggles one, second click removes it', () => {
  let r = nextSelection(new Set(), 9, { order }); assert.deepStrictEqual(ids(r), [9])
  r = nextSelection(r.picked, 9, { order, anchor: r.anchor }); assert.deepStrictEqual(ids(r), [])
})
t('⇧-click adds the range from the anchor, both directions, anchor kept', () => {
  let r = nextSelection(new Set(), 9, { order })
  r = nextSelection(r.picked, 6, { order, anchor: r.anchor, shift: true }); assert.deepStrictEqual(ids(r), [9, 8, 7, 6]); assert.strictEqual(r.anchor, 9)
  r = nextSelection(new Set(), 7, { order }); r = nextSelection(r.picked, 10, { order, anchor: r.anchor, shift: true }); assert.deepStrictEqual(ids(r), [10, 9, 8, 7])
})
t('⇧-click without an anchor behaves like a click', () => {
  assert.deepStrictEqual(ids(nextSelection(new Set(), 8, { order, shift: true })), [8])
})
t('confirmation wording (Lỗi 2 wording, no "recording files")', () => {
  assert.deepStrictEqual(confirmText(3), { title: 'Delete 3 meetings?', body: "Notes, transcript and MoM will be removed. This can't be undone." })
  assert.strictEqual(confirmText(1).title, 'Delete 1 meeting?'); assert.ok(!/recording/i.test(confirmText(2).body))
})
t('result line says what was skipped', () => {
  assert.strictEqual(resultText({ ok: true, deleted: [1, 2, 3], skipped_running: [4, 5], unknown: [] }), 'Deleted 3 meetings · 2 meetings still processing were skipped')
  assert.strictEqual(resultText({ ok: true, deleted: [1], skipped_running: [], unknown: [9] }), 'Deleted 1 meeting · 1 meeting already gone')
  assert.strictEqual(resultText({ ok: false, error: 'daemon connection closed' }), 'daemon connection closed')
})
console.log(`${pass}/5 PASS`)
