// Self-check for main/meeting_detector.js (Lỗi 7): feeds synthetic probe snapshots with a fake clock and asserts the
// the reference app state machine. Run: node tools/meeting_detector_check.js
const assert = require('assert')
const { createDetector, parse, isMeetingAppBundleId, micInputEffective } = require('../main/meeting_detector.js')

const TEAMS = 'com.microsoft.teams2', ZOOM = 'us.zoom.xos'
const snap = (front, procs = [], mic = procs.some((p) => p.mic) ? 'active' : 'inactive') =>
  ({ mic, speaker: 'inactive', frontmostBundleId: front, frontmostName: front, processes: [{ pid: 1, bundleId: 'com.apple.finder', mic: false, speaker: false }, ...procs] })
const inCall = (id, spk = true) => ({ pid: 2, bundleId: id, mic: true, speaker: spk })
const helperOf = (id) => ({ pid: 3, bundleId: id + '.helper', mic: true, speaker: true })   // Teams/Zoom often hold the mic in a helper

function rig(recording = () => false) {
  let t = 0
  const out = { detected: [], events: [], logs: [] }
  const d = createDetector({ now: () => t, isRecording: recording, probe: async () => snap(''), onDetected: (m) => out.detected.push(m), onEvent: (e) => out.events.push(e), log: (m) => out.logs.push(m) })
  const poll = (s, dt = 3000) => { t += dt; d.pollOnce(s) }
  return { d, out, poll }
}
let n = 0
const test = (name, fn) => { fn(); n++; console.log('PASS', name) }

test('known ids / helper processes match by keyword', () => {
  assert(isMeetingAppBundleId(TEAMS) && isMeetingAppBundleId(ZOOM) && isMeetingAppBundleId('us.zoom.CptHost') && !isMeetingAppBundleId('com.google.Chrome'))
})
test('parse keeps the reference app shape, drops entries without bundle id, bad JSON -> error', () => {
  const p = parse('{"mic":"active","speaker":"bogus","frontmostBundleId":"x","processes":[{"pid":5,"bundleId":"","mic":true},{"pid":6,"bundleId":"a","mic":true}]}')
  assert.strictEqual(p.mic, 'active'); assert.strictEqual(p.speaker, 'error'); assert.strictEqual(p.processes.length, 1)
  assert.strictEqual(parse('nope').mic, 'error')
})
test('Teams frontmost + mic + speaker -> popup after 2 polls (FAST_CONFIRM_POLLS)', () => {
  const { d, out, poll } = rig()
  poll(snap(TEAMS, [inCall(TEAMS)])); assert.strictEqual(d.state, 'confirming'); assert.strictEqual(out.detected.length, 0)
  poll(snap(TEAMS, [inCall(TEAMS)])); assert.strictEqual(out.detected.length, 1); assert.strictEqual(d.state, 'active')
  assert.strictEqual(out.detected[0].appLabel, 'Microsoft Teams'); assert.strictEqual(out.detected[0].sourceId, 'bundle:' + TEAMS)
})
test('mic via a helper process of the same app counts (keyword match)', () => {
  const { out, poll } = rig()
  poll(snap(ZOOM, [helperOf(ZOOM)])); poll(snap(ZOOM, [helperOf(ZOOM)])); assert.strictEqual(out.detected.length, 1)
})
test('mic without speaker -> needs 5 polls (SLOW_CONFIRM_POLLS)', () => {
  const { out, poll } = rig()
  for (let i = 0; i < 4; i++) poll(snap(ZOOM, [inCall(ZOOM, false)]))
  assert.strictEqual(out.detected.length, 0)
  poll(snap(ZOOM, [inCall(ZOOM, false)])); assert.strictEqual(out.detected.length, 1)
})
test('meeting app frontmost without mic -> watching; non-meeting app -> idle, never a popup', () => {
  const { d, out, poll } = rig()
  poll(snap(TEAMS)); assert.strictEqual(d.state, 'watching')
  poll(snap('com.google.Chrome', [inCall('com.google.Chrome')])); assert.strictEqual(d.state, 'idle'); assert.strictEqual(out.detected.length, 0)
})
test('mic dropped while confirming -> cancelled', () => {
  const { d, out, poll } = rig()
  poll(snap(ZOOM, [inCall(ZOOM, false)])); poll(snap(ZOOM, [])); assert.strictEqual(d.state, 'watching'); assert.strictEqual(out.detected.length, 0)
})
test('dismissed -> no second popup in the same call; again after the mic was released >= 20 s', () => {
  const { d, out, poll } = rig()
  poll(snap(TEAMS, [inCall(TEAMS)])); poll(snap(TEAMS, [inCall(TEAMS)])); assert.strictEqual(out.detected.length, 1)
  d.dismiss('bundle:' + TEAMS); assert.strictEqual(d.state, 'idle')
  for (let i = 0; i < 5; i++) poll(snap(TEAMS, [inCall(TEAMS)]))
  assert.strictEqual(out.detected.length, 1)                                  // still suppressed
  poll(snap(TEAMS, []), 10000); poll(snap(TEAMS, []), 11000)                    // mic released 21 s
  poll(snap(TEAMS, [inCall(TEAMS)])); poll(snap(TEAMS, [inCall(TEAMS)]))
  assert.strictEqual(out.detected.length, 2)
})
test('detected but never recorded: mic released 2 polls -> expires quietly to idle', () => {
  const { d, out, poll } = rig(() => false)
  poll(snap(TEAMS, [inCall(TEAMS)])); poll(snap(TEAMS, [inCall(TEAMS)]))
  poll(snap(TEAMS, []), 10000); assert.strictEqual(d.state, 'active')
  poll(snap(TEAMS, []), 10000); assert.strictEqual(d.state, 'idle'); assert.strictEqual(out.events.filter((e) => e.type === 'meeting_ended').length, 0)
})
test('recording: app releases the mic 2 polls (20 s) -> exactly one meeting_ended', () => {
  let rec = false
  const { out, poll } = rig(() => rec)
  poll(snap(TEAMS, [inCall(TEAMS)])); poll(snap(TEAMS, [inCall(TEAMS)])); rec = true
  poll(snap(TEAMS, [inCall(TEAMS)]), 10000)
  poll(snap(TEAMS, []), 10000); assert.strictEqual(out.events.filter((e) => e.type === 'meeting_ended').length, 0)
  poll(snap(TEAMS, []), 10000); poll(snap(TEAMS, []), 10000); poll(snap(TEAMS, []), 10000)
  assert.strictEqual(out.events.filter((e) => e.type === 'meeting_ended').length, 1)
})
test('manual Record while Zoom holds the mic -> app identified in active, meeting_ended when it leaves', () => {
  let rec = true
  const { d, out, poll } = rig(() => rec)
  d.start(); d.meetingStarted()                                                // as main does on Record (start() only queues an async poll)
  poll(snap('com.apple.finder', [inCall(ZOOM)]), 10000); assert.strictEqual(d.state, 'active')
  poll(snap('com.apple.finder', []), 10000); poll(snap('com.apple.finder', []), 10000)
  assert.strictEqual(out.events.filter((e) => e.type === 'meeting_ended').length, 1)
  d.stop()
})
test('global-mic fallback when the probe has no per-process list', () => {
  const { out, poll } = rig()
  const g = (front) => ({ mic: 'active', speaker: 'active', frontmostBundleId: front, frontmostName: '', processes: [] })
  poll(g(TEAMS)); poll(g(TEAMS)); assert.strictEqual(out.detected.length, 1)
})
test('Lỗi 9 — the 3 cases anh listed (appMicOn = meeting app unmuted signal)', () => {
  assert.strictEqual(micInputEffective({ appMicOn: false, button: false }), false)   // 1/ muted in Teams + Middy mic off -> nothing
  assert.strictEqual(micInputEffective({ appMicOn: false, button: true }), true)     // 2/ muted in Teams + Middy mic on  -> mic
  assert.strictEqual(micInputEffective({ appMicOn: true, button: false }), true)     // 3/ unmuted in Teams -> always mic
  assert.strictEqual(micInputEffective({ appMicOn: true, button: true }), true)
  assert.strictEqual(micInputEffective({ appMicOn: null, button: false }), false)    // today: no trustworthy mute signal -> button decides
  assert.strictEqual(micInputEffective({ appMicOn: null, button: undefined }), true) // settings from before Lỗi 9 -> mic on (old behaviour)
})
console.log(n + '/' + n + ' PASS')
