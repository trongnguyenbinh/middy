// the reference app protocol v2 client (spec, applySlidingWindow): {windowOffset, window[], settled?} events from the daemon.
// entries[0:windowOffset] stay as they are, `window` replaces the tail, `settled` (when present) replaces the whole prefix.
export function applyWindow(entries, ev) {
  const off = ev.windowOffset || 0
  let head = ev.settled ? ev.settled : entries.slice(0, off)
  return head.concat(ev.window || [])
}

// the reference app display rule (spec A5 + M1 blocks.lines): consecutive blocks of the same speaker merge into one bubble; a bubble
// closes when it has >= 30 words and ends a sentence. Interim (listening) text of a source is shown as its own faded bubble.
export function bubbles(entries, interim) {
  const out = []
  let cur = null
  let lastSys = 'Speaker 1'
  for (const b of entries) {
    if (!b.text) continue
    if (b.speaker && b.source !== 'mic') lastSys = b.speaker
    const spk = b.speaker || (b.source === 'mic' ? 'You' : lastSys)      // unlabelled system block: keep the last known speaker
    if (cur && cur.speaker === spk && cur.source === b.source) cur.text += ' ' + b.text
    else { if (cur) out.push(cur); cur = { speaker: spk, source: b.source, text: b.text, state: b.state, id: b.id } }
    cur.state = b.state
    const words = cur.text.split(/\s+/).length
    if (words >= 30 && /[.?!。？！]$/.test(cur.text.trim())) { out.push(cur); cur = null }
  }
  if (cur) out.push(cur)
  for (const [source, text] of Object.entries(interim || {})) if (text) out.push({ speaker: source === 'mic' ? 'You' : 'Speaker ?', source, text, state: 'listening', id: 'i-' + source })
  return out
}

const COLORS = ['#7c6cf5', '#2bb6a3', '#e94f9c', '#f5a623', '#4f9be9', '#c7da35', '#f06b4f', '#9b59b6', '#3fbf6b', '#e0c341']
export function avatarColor(name) {          // the reference app: colour hashed from 10 colours
  let h = 0
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return COLORS[h % COLORS.length]
}
