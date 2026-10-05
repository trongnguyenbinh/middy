// Pure renderer logic: the v2 transcript window client, the bubble display rule, the transcript export, markdown -> HTML.
import test from 'node:test'
import assert from 'node:assert'
import { applyWindow, bubbles, avatarColor } from '../renderer/lib/transcript.js'
import { transcriptText } from '../renderer/lib/transcript_export.js'
import { mdToHtml } from '../renderer/lib/md.js'

const blk = (id, text, extra = {}) => ({ id, source: 'system', speaker: 'Speaker 1', text, state: 'refined', ...extra })

test('applyWindow keeps the prefix, replaces the tail, settled replaces the whole prefix', () => {
  let e = applyWindow([], { windowOffset: 0, window: [blk(0, 'a'), blk(1, 'b')] })
  e = applyWindow(e, { windowOffset: 1, window: [blk(1, 'B'), blk(2, 'c')] })
  assert.deepStrictEqual(e.map((x) => x.text), ['a', 'B', 'c'])
  e = applyWindow(e, { windowOffset: 2, settled: [blk(0, 'A'), blk(1, 'B')], window: [] })
  assert.deepStrictEqual(e.map((x) => x.text), ['A', 'B'])
  assert.deepStrictEqual(applyWindow(e, {}), [])
})

test('bubbles merge one speaker, break at 30 words + end punctuation, show interim', () => {
  const long = Array.from({ length: 30 }, (_, i) => 'w' + i).join(' ') + '.'
  const out = bubbles([blk(0, 'hello'), blk(1, long), blk(2, 'next'), blk(3, 'me', { source: 'mic', speaker: '' }), blk(4, '')], { system: 'typing', mic: '' })
  assert.deepStrictEqual(out.map((b) => [b.speaker, b.text.split(' ').length]), [['Speaker 1', 31], ['Speaker 1', 1], ['You', 1], ['Speaker ?', 1]])
  assert.strictEqual(out.at(-1).state, 'listening')
})

test('an unlabelled system block keeps the last known speaker', () => {
  const out = bubbles([blk(0, 'x', { speaker: 'Speaker 2' }), blk(1, 'y', { speaker: '', source: 'mic' }), blk(2, 'z', { speaker: '' })])
  assert.deepStrictEqual(out.map((b) => b.speaker), ['Speaker 2', 'You', 'Speaker 2'])
})

test('avatarColor is stable and from the palette', () => {
  assert.strictEqual(avatarColor('Speaker 1'), avatarColor('Speaker 1'))
  assert.match(avatarColor('Ánh'), /^#[0-9a-f]{6}$/)
})

test('transcriptText: md and txt, dropped segments, hh:mm:ss', () => {
  const m = { name: 'Họp', language: 'Vietnamese', started_at: 0, audio_end_s: 3725 }
  const segs = [{ s: 5, e: 9, speaker: 'Speaker 1', text: 'xin chào' }, { s: 3700, e: 3712, dropped_lang: 1, text: '' }, { s: 3720, e: 3725, speaker: '', text: 'ok' }]
  const md = transcriptText(m, segs, 'md'), txt = transcriptText(m, segs, 'txt')
  assert.ok(md.startsWith('# Họp — transcript') && md.includes('01:02:05 · 3 segments'))
  assert.ok(md.includes('**[00:00:05] Speaker 1:** xin chào') && md.includes('[dropped: ~12 s in another language]'))
  assert.ok(txt.includes('[01:02:00] Speaker ?: ok') && !txt.includes('**'))
  assert.ok(transcriptText(m, null, 'txt').includes('0 segments'))
})

test('mdToHtml: headings, lists, tasks, tables, inline, escaping', () => {
  const h = mdToHtml('# T\n\n- a **b**\n- [x] done\n1. one\n| A | B |\n|---|---|\n| 1 | <x> |\npara `c` *i*')
  assert.ok(h.includes('<h1>T</h1>'))
  assert.ok(h.includes('<ul>\n<li>a <strong>b</strong></li>'))
  assert.ok(h.includes('<li data-type="taskItem" data-checked="true">done</li>'))
  assert.ok(h.includes('<ol>\n<li>one</li>\n</ol>'))
  assert.ok(h.includes('<th>A</th>') && h.includes('<td>&lt;x&gt;</td>'))
  assert.ok(h.includes('<p>para <code>c</code> <em>i</em></p>'))
  assert.strictEqual(mdToHtml(''), '')
  assert.strictEqual(mdToHtml(null), '')
})
