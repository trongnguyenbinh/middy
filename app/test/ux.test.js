const test = require('node:test')
const assert = require('node:assert')
const { trayState, onSomeDisplay } = require('../main/ux.js')

test('menu bar icon: idle, listening, paused, getting ready, writing up, detected', () => {
  assert.deepStrictEqual(trayState({ state: 'idle' }, 0, null), { title: '', tip: 'Middy' })
  assert.strictEqual(trayState({ state: 'recording' }, 0, null).title, '●')
  assert.strictEqual(trayState({ state: 'recording', paused: true }, 0, null).title, '❚❚')
  assert.strictEqual(trayState({ state: 'starting' }, 0, null).tip, 'Middy: getting ready')
  assert.deepStrictEqual(trayState({ state: 'idle' }, 1, null), { title: '…', tip: 'Middy: writing up the last meeting' })
  assert.strictEqual(trayState({ state: 'recording' }, 1, null).title, '●')                     // a new meeting wins over the old write-up
  assert.strictEqual(trayState({ state: 'idle' }, 0, { appLabel: 'Zoom' }).tip, 'Middy: Zoom meeting detected')
})

test('overlay position is reused only on a connected display', () => {
  const main = { x: 0, y: 25, width: 1440, height: 875 }, right = { x: 1440, y: 0, width: 1920, height: 1080 }
  assert.ok(onSomeDisplay({ x: 100, y: 100 }, [main]))
  assert.ok(onSomeDisplay({ x: 2000, y: 500 }, [main, right]))
  assert.ok(!onSomeDisplay({ x: 2000, y: 500 }, [main]))                                     // that monitor was unplugged
  assert.ok(!onSomeDisplay({ x: 1420, y: 100 }, [main]))                                     // grab bar off-screen
  assert.ok(!onSomeDisplay(null, [main]))
})
