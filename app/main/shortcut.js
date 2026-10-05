// Việc 16 (29/09): one system-wide shortcut, default ⌃⌥R — not in a meeting ⇒ Start (same path as the toolbar's Record),
// in a meeting ⇒ Stop (same path as End meeting, finishes in the background).
// Pure logic here, tested by tools/shortcut_check.js.
const DEFAULT_SHORTCUT = 'Control+Alt+R'
const DEBOUNCE_MS = 1000                    // a double press must not Start and immediately Stop

// A key press from the Settings recorder -> Electron accelerator. Uses `code` (physical key), not `key`: ⌥R types "®".
function accelFromKey({ code, metaKey, ctrlKey, altKey, shiftKey }) {
  let k
  if (/^Key[A-Z]$/.test(code)) k = code.slice(3)
  else if (/^Digit[0-9]$/.test(code)) k = code.slice(5)
  else if (/^F([1-9]|1[0-9])$/.test(code)) k = code
  else k = { Space: 'Space', ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right', Minus: '-', Equal: '=', Comma: ',', Period: '.', Slash: '/' }[code] || null
  if (!k) return { error: 'Use a letter, digit, F-key, arrow, Space or punctuation key' }
  const mods = [metaKey && 'Command', ctrlKey && 'Control', altKey && 'Alt', shiftKey && 'Shift'].filter(Boolean)
  if (!(metaKey || ctrlKey || altKey) && !/^F/.test(k)) return { error: 'Add ⌘, ⌃ or ⌥ so normal typing is not caught' }
  return { accelerator: [...mods, k].join('+') }
}

const SYMBOL = { Command: '⌘', Control: '⌃', Alt: '⌥', Shift: '⇧' }
const label = (acc) => (acc || '').split('+').map((p) => SYMBOL[p] || p).join('')

function createShortcut({ globalShortcut, isMeeting, start, stop, log = () => {}, now = Date.now }) {
  let last = -Infinity
  const state = { accelerator: null, ok: false, error: null }
  function onPress() {
    const t = now()
    if (t - last < DEBOUNCE_MS) { log('shortcut: ignored (double press)'); return 'ignored' }
    last = t
    if (isMeeting()) { log('shortcut: stop'); stop(); return 'stop' }
    log('shortcut: start'); start(); return 'start'
  }
  // register `acc`; on failure keep the previous one (if any) working and say why
  function register(acc) {
    const prev = state.accelerator
    if (prev) globalShortcut.unregister(prev)
    let ok = false, error = null
    try { ok = globalShortcut.register(acc, onPress) } catch (e) { error = String(e.message || e) }
    if (ok) { Object.assign(state, { accelerator: acc, ok: true, error: null }); log('shortcut registered ' + acc); return { ...state } }
    error = error || `${label(acc)} is already used by another app or by macOS`
    log('shortcut NOT registered ' + acc + ': ' + error)
    if (prev) { const back = globalShortcut.register(prev, onPress); Object.assign(state, { accelerator: prev, ok: back }) } else Object.assign(state, { accelerator: acc, ok: false })
    state.error = error
    return { ...state, rejected: acc }
  }
  return { register, onPress, state: () => ({ ...state, label: label(state.accelerator) }) }
}

module.exports = { DEFAULT_SHORTCUT, DEBOUNCE_MS, accelFromKey, label, createShortcut }
