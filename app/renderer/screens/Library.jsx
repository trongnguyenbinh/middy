// "My spaces" library
// design in the same colour system: meetings grouped by space on the left, note (editable) / transcript on the right,
// full-text search (SQLite FTS5 in the daemon), export as .md.
import React, { useEffect, useState } from 'react'
import { api } from '../index.jsx'
import { NoteEditor } from './NoteEditor.jsx'
import { mdToHtml, htmlToMd } from '../lib/md.js'
import { nextSelection, confirmText, resultText } from '../lib/select.mjs'
import { transcriptText } from '../lib/transcript_export.js'

// FTS5 snippet arrives as plain text with [ ] around matches (store.search); render as text nodes, never as HTML (rnd M2 item 6)
const snippetNodes = (t) => (t || '').split(/(\[[^\]]*\])/).map((p, i) => /^\[.*\]$/.test(p) ? <b key={i}>{p.slice(1, -1)}</b> : p)
const fmt = (t) => new Date(t * 1000).toLocaleString()
const dur = (s) => `${Math.floor(s / 60)} min`
// Lỗi 14: meetings finish in the background after Stop; the list says where each one is, and errors are shown, never hidden
const busy = (st) => st === 'recording' || st === 'finishing'
const statusText = (st) => st === 'finishing' ? 'writing MoM…' : st

let flashTimer = null                              // one status message at a time (Export Word can take 40 s)
export function Library() {
  const [meetings, setMeetings] = useState([])
  const [sel, setSel] = useState(null)
  const [data, setData] = useState(null)
  const [tab, setTab] = useState('note')
  const [q, setQ] = useState('')
  const [hits, setHits] = useState(null)
  const [html, setHtml] = useState('')
  const [dirty, setDirty] = useState(false)
  const [msg, setMsg] = useState('')
  const [confirmDel, setConfirmDel] = useState(false)
  // Việc 18: bulk delete. "Select" (or ⌘/⇧-click) -> pick meetings -> "Delete (n)" -> one confirmation; busy meetings are skipped
  const [selMode, setSelMode] = useState(false)
  const [picked, setPicked] = useState(new Set())
  const [anchor, setAnchor] = useState(null)
  const [confirmBulk, setConfirmBulk] = useState(false)
  const endSelect = () => { setSelMode(false); setPicked(new Set()); setAnchor(null) }
  const delBulk = async () => {
    const ids = [...picked]
    const r = await api.invoke('daemon', { cmd: 'delete_meetings', meeting_ids: ids })
    setConfirmBulk(false)
    if (r.ok && r.deleted.includes(sel)) { setSel(null); setData(null) }
    flash(resultText(r), r.ok && !r.skipped_running.length ? 2500 : 6000)
    if (r.ok) endSelect()
    load()
  }
  const del = async () => {                      // bug 2: delete meeting + notes + transcript + files (irreversible)
    const r = await api.invoke('daemon', { cmd: 'delete_meeting', meeting_id: sel })
    setConfirmDel(false)
    if (r.ok) { setSel(null); setData(null); setMsg('Deleted'); setTimeout(() => setMsg(''), 1500); load() } else { setMsg(r.error || 'Delete failed'); setTimeout(() => setMsg(''), 3000) }
  }
  const load = () => api.invoke('daemon', { cmd: 'meetings' }).then((r) => r.ok && setMeetings(r.meetings))
  useEffect(() => { load(); return api.on('event', (ev) => { if (ev.type === 'done') load() }) }, [])
  const anyBusy = meetings.some((m) => busy(m.status))
  useEffect(() => { if (!anyBusy) return; const t = setInterval(load, 3000); return () => clearInterval(t) }, [anyBusy])
  const selStatus = meetings.find((m) => m.id === sel)?.status
  useEffect(() => api.on('ui', (c) => { if (c === 'select:last' && meetings.length) setSel(meetings[0].id); if (c === 'delete') setConfirmDel(true); if (c === 'confirm-delete') del(); if (c === 'export-transcript:md' && data) exportTranscript('md'); if (c === 'export-transcript:txt' && data) exportTranscript('txt') }), [meetings, sel, data])
  useEffect(() => {
    if (!sel) return
    api.invoke('daemon', { cmd: 'meeting', meeting_id: sel }).then((r) => {
      if (!r.ok) return
      setData(r)
      const user = r.notes.find((n) => n.kind === 'user'), mom = r.notes.find((n) => n.kind === 'mom'), live = r.notes.filter((n) => n.kind === 'live').slice(-1)[0]
      setHtml(mdToHtml((user || mom || live || {}).text || '')); setDirty(false)
    })
  }, [sel, selStatus])                              // reload when its MoM lands
  useEffect(() => { if (!q.trim()) { setHits(null); return } const t = setTimeout(() => api.invoke('daemon', { cmd: 'search', q }).then((r) => r.ok && setHits(r.hits)), 300); return () => clearTimeout(t) }, [q])
  const save = async () => { await api.invoke('daemon', { cmd: 'save_note', meeting_id: sel, text: htmlToMd(html) }); setDirty(false); setMsg('Saved'); setTimeout(() => setMsg(''), 1500) }
  const flash = (m, ms = 1500) => { setMsg(m); clearTimeout(flashTimer); flashTimer = setTimeout(() => setMsg(''), ms) }
  const exportNote = async () => { const r = await api.invoke('export:file', { title: data.meeting.name, ext: 'md', text: `# ${data.meeting.name}\n\n${htmlToMd(html)}` }); if (r.ok) flash('Exported') }
  const exportWord = async () => { flash('Writing Word MoM…', 60000); const r = await api.invoke('export:docx', { meetingId: sel, title: data.meeting.name }); flash(r.ok ? (r.warn?.length ? 'Word MoM saved — check: ' + r.warn.join(', ') : 'Word MoM saved') : r.canceled ? '' : 'Word export failed: ' + r.error, r.ok ? 3000 : 6000) }
  const exportTranscript = async (ext) => { const r = await api.invoke('export:file', { title: data.meeting.name + ' - transcript', ext, text: transcriptText(data.meeting, data.segments, ext) }); if (r.ok) flash('Exported') }
  const groups = {}
  for (const m of meetings) (groups[m.space || 'default'] ||= []).push(m)
  return (
    <div className="library">
      <aside className="lib-side">
        <div className="lib-brand"><span className="logo-m">M</span><span>My spaces</span><span className="grow" />
          {!hits && meetings.length > 0 && (selMode
            ? <><button className="btn-ghost small" onClick={() => setPicked(new Set(meetings.map((m) => m.id)))}>Select all</button><button className="btn-ghost small" onClick={endSelect}>Cancel</button></>
            : <button className="btn-ghost small" title="Select several meetings (or ⌘/⇧-click)" onClick={() => setSelMode(true)}>Select</button>)}
        </div>
        {msg && !data && <div className={'small pad-x ' + (/skipped|failed|closed/i.test(msg) ? 'danger' : 'muted')}>{msg}</div>}
        {selMode && <div className="row pad-x"><span className="muted small grow">{picked.size} selected</span><button className="btn-ghost danger small" disabled={!picked.size} onClick={() => setConfirmBulk(true)}>Delete ({picked.size})</button></div>}
        <input className="field" placeholder="Search notes and transcripts…" value={q} onChange={(e) => setQ(e.target.value)} />
        {hits ? (
          <div className="lib-list">{hits.length === 0 && <div className="muted pad">No results</div>}{hits.map((h, i) => <button key={i} className="lib-item" onClick={() => { setSel(h.meeting_id); setTab('transcript') }}><div className="muted small">{h.name || 'meeting ' + h.meeting_id} · {Math.floor(h.s / 60)}:{String(Math.floor(h.s % 60)).padStart(2, '0')}</div><div className="snip">{snippetNodes(h.snippet)}</div></button>)}</div>
        ) : (
          <div className="lib-list">{Object.entries(groups).map(([space, ms]) => (
            <div key={space}><div className="label">{space.toUpperCase()}</div>{ms.map((m) => (
              <button key={m.id} className={'lib-item' + ((selMode ? picked.has(m.id) : m.id === sel) ? ' on' : '')} onClick={(e) => {
                if (!selMode && !e.metaKey && !e.shiftKey) { setSel(m.id); return }
                const order = Object.values(groups).flat().map((x) => x.id)             // as listed on screen, for ⇧-click ranges
                const r = nextSelection(picked, m.id, { shift: e.shiftKey, order, anchor }); setSelMode(true); setPicked(r.picked); setAnchor(r.anchor)
              }}><div>{selMode && <span className="pick">{picked.has(m.id) ? '☑' : '☐'}</span>}{m.name}</div><div className={'small ' + (m.status?.startsWith('error') ? 'danger' : 'muted')}>{fmt(m.started_at)} · {dur(m.audio_end_s)} · {statusText(m.status)}</div></button>
            ))}</div>
          ))}{meetings.length === 0 && <div className="muted pad">No meetings yet. Press Record on the toolbar.</div>}</div>
        )}
      </aside>
      <main className="lib-main">
        {!data ? <div className="muted center pad">Select a meeting</div> : (
          <>
            <div className="lib-head">
              <input className="pv-title big" value={data.meeting.name} onChange={(e) => { setData({ ...data, meeting: { ...data.meeting, name: e.target.value } }) }} onBlur={() => api.invoke('daemon', { cmd: 'set_name', meeting_id: sel, name: data.meeting.name }).then(load)} />
              <div className="tabs text"><button className={tab === 'note' ? 'on' : ''} onClick={() => setTab('note')}>Note</button><button className={tab === 'transcript' ? 'on' : ''} onClick={() => setTab('transcript')}>Transcript</button></div>
              <span className="grow" /><span className="muted small">{msg}</span>
              {tab === 'note' && <button className="btn-accent" onClick={save} disabled={!dirty}>Save</button>}
              <button className="btn-ghost" onClick={exportNote} title="Export the note as Markdown">Export note</button>
              <button className="btn-ghost" onClick={exportWord} disabled={msg === 'Writing Word MoM…'} title="MoM in the company Word form (.docx), written on this Mac">Export Word</button>
              <span className="split"><button className="btn-ghost" onClick={() => exportTranscript('md')} title="Original transcript with [hh:mm:ss] and speakers">Export transcript .md</button><button className="btn-ghost" onClick={() => exportTranscript('txt')}>.txt</button></span>
              <button className="btn-ghost danger" title="Delete this meeting" onClick={() => setConfirmDel(true)}>Delete</button>
            </div>
            {busy(selStatus) && <div className="muted small pad-x">Writing the MoM in the background — it appears here when ready.</div>}
            {selStatus?.startsWith('error') && <div className="danger small pad-x">{selStatus}</div>}
            <div className="muted small pad-x">{fmt(data.meeting.started_at)} · {data.meeting.language} · {dur(data.meeting.audio_end_s)} · space {data.meeting.space}</div>
            {tab === 'note' ? (
              <div className="lib-editor"><NoteEditor html={html} onChange={(h) => { setHtml(h); setDirty(true) }} placeholder="Type your notes here..." />
                {data.notes.some((n) => n.kind === 'ask') && (                       /* Việc 17: questions asked during the meeting */
                  <div className="pad-x"><div className="label">ASKED DURING THE MEETING</div>
                    {data.notes.filter((n) => n.kind === 'ask').sort((a, b) => a.idx - b.idx).map((n) => { let qa = {}; try { qa = JSON.parse(n.text) } catch {} return (
                      <div key={n.idx} className="card"><div><b>{qa.q}</b></div><div className="prose" dangerouslySetInnerHTML={{ __html: mdToHtml(qa.a || '') }} /></div>) })}
                  </div>)}
              </div>
            ) : (
              <div className="lib-transcript">{(data.segments || []).map((s) => (
                <div key={s.seq} className={'line' + (s.dropped_lang ? ' muted' : '')}><span className="ts">{Math.floor(s.s / 60)}:{String(Math.floor(s.s % 60)).padStart(2, '0')}</span><span className="spk">{s.speaker || 'Speaker ?'}</span><span>{s.dropped_lang ? `[dropped: ~${Math.round(s.e - s.s)} s in another language]` : s.text}</span></div>
              ))}</div>
            )}
          </>
        )}
      </main>
      {confirmBulk && (
        <div className="veil"><div className="dialog"><h3>{confirmText(picked.size).title}</h3><p className="muted">{confirmText(picked.size).body}</p><div className="row"><button className="btn-danger" onClick={delBulk}>Delete</button><button className="btn-ghost" onClick={() => setConfirmBulk(false)}>Keep</button></div></div></div>
      )}
      {confirmDel && data && (
        <div className="veil"><div className="dialog"><h3>Delete this meeting?</h3><p className="muted">"{data.meeting.name}" — notes and transcript on this Mac will be removed. This cannot be undone.</p><div className="row"><button className="btn-danger" onClick={del}>Delete</button><button className="btn-ghost" onClick={() => setConfirmDel(false)}>Keep</button></div></div></div>
      )}
    </div>
  )
}
