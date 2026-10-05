// Việc 18: multi-select in the Library list, the macOS way.
// Click / ⌘-click toggles one meeting and moves the anchor; ⇧-click adds the whole range from the anchor (anchor kept).
export function nextSelection(picked, id, { shift = false, order = [], anchor = null } = {}) {
  const s = new Set(picked)
  if (shift && anchor != null && order.includes(anchor) && order.includes(id)) {
    const [a, b] = [order.indexOf(anchor), order.indexOf(id)].sort((x, y) => x - y)
    for (const x of order.slice(a, b + 1)) s.add(x)
    return { picked: s, anchor }
  }
  if (s.has(id)) s.delete(id); else s.add(id)
  return { picked: s, anchor: id }
}

const n = (k, word) => `${k} ${word}${k === 1 ? '' : 's'}`

// the one confirmation (wording of the single delete, Lỗi 2) and the result line, skipped meetings said out loud
export const confirmText = (k) => ({ title: `Delete ${n(k, 'meeting')}?`, body: "Notes, transcript and MoM will be removed. This can't be undone." })
export function resultText(r) {
  if (!r || !r.ok) return (r && r.error) || 'Delete failed'
  const parts = [`Deleted ${n(r.deleted.length, 'meeting')}`]
  if (r.skipped_running.length) parts.push(`${n(r.skipped_running.length, 'meeting')} still processing ${r.skipped_running.length === 1 ? 'was' : 'were'} skipped`)
  if (r.unknown.length) parts.push(`${n(r.unknown.length, 'meeting')} already gone`)
  return parts.join(' · ')
}
