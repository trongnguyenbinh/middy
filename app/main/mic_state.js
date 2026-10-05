// Lỗi 9b (anh chose A, 28/09): is the meeting app's own mic ON? Read-only, through the Swift helper tools/mic_state.swift, which
// lists the labels of mic / mute controls (Accessibility API). This file turns those labels into appMicOn for
// micInputEffective() in main/meeting_detector.js:
//   true  -> the app shows a "Mute" control, i.e. anh is UNMUTED there  -> Middy always takes the mic (case 3)
//   false -> the app shows an "Unmute" control, i.e. anh is MUTED        -> Middy's Mic on/off button decides (cases 1-2)
//   null  -> not trusted / app not running / no control / contradicting labels -> the button decides (never a wrong "off")
// JS \b is ASCII-only, so the VI patterns use (?![a-z]) instead. A label says what the control WILL do: "Unmute" is shown while muted. EN + VI (VI strings are a guess until seen for real).
const { spawn } = require('child_process')

const EXCLUDE = /notification|chat|\ball\b|everyone|participant|speaker|video|camera|conversation|channel|thông báo|tất cả|mọi người|người tham gia|loa/i
const SAYS_MUTED = [/\bunmute\b/i, /bật tiếng/i, /bỏ tắt tiếng/i, /bật (micrô|mic)(?![a-z])/i, /turn on (the )?(mic|microphone)\b/i]   // control would unmute
const SAYS_UNMUTED = [/\bmute\b/i, /tắt tiếng/i, /tắt (micrô|mic)(?![a-z])/i, /turn off (the )?(mic|microphone)\b/i]                    // control would mute
const MIC_CONTEXT = /\bmic\b|micro|audio|âm thanh|micrô/i

function classifyOne(label) {
  if (!label || EXCLUDE.test(label)) return null
  const bare = /^\s*(un)?mute\s*(\(.*\))?\s*$/i.test(label.split('|')[0])     // Zoom's toolbar says just "Mute" / "Unmute"
  if (!bare && !MIC_CONTEXT.test(label) && !/tiếng/i.test(label)) return null
  if (SAYS_MUTED.some((r) => r.test(label))) return false                       // test "unmute" before "mute"
  if (SAYS_UNMUTED.some((r) => r.test(label))) return true
  return null
}

// controls: [{ role, label }] from the helper -> { appMicOn: true|false|null, votes }
function classify(controls) {
  const votes = (controls || []).map((c) => classifyOne(c.label)).filter((v) => v !== null)
  const states = new Set(votes)
  return { appMicOn: states.size === 1 ? votes[0] : null, votes: votes.length, conflict: states.size > 1 }
}

// Runs the helper for one meeting app while a meeting records. onState({ appMicOn, trusted, running, controls, conflict }).
function createMicStateWatcher({ binary, bundleId, onState, log = () => {}, args = [] }) {
  const p = spawn(binary, ['--bundle', bundleId, ...args], { stdio: ['ignore', 'pipe', 'ignore'] })
  let buf = '', last = null
  p.stdout.on('data', (d) => {
    buf += d; let i
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1)
      let o; try { o = JSON.parse(line) } catch { continue }
      const c = o.trusted && o.running ? classify(o.controls) : { appMicOn: null, votes: 0, conflict: false }
      const st = { appMicOn: c.appMicOn, trusted: !!o.trusted, running: !!o.running, controls: (o.controls || []).length, conflict: c.conflict }
      const key = JSON.stringify(st)
      if (key !== last) { last = key; onState(st) }
    }
  })
  p.on('exit', (code) => { log('mic-state helper exited ' + code); onState({ appMicOn: null, trusted: null, running: false, controls: 0, conflict: false, exited: true }) })
  return { stop() { try { p.kill() } catch {} }, pid: p.pid }
}

module.exports = { classify, classifyOne, createMicStateWatcher }
