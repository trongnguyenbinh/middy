// Preview: header Back · Save · title · template dropdown (glowing #C7DA35 border) · X; "CHOOSE A SPACE TO SAVE THIS NOTE" /
// "Pick or create one"; read-only note; after Save: "Saved" + Done / Open in library; closing unsaved: "You'll lose this note".
import React, { useEffect, useState } from 'react'
import { api, useMeeting } from '../index.jsx'
import { mdToHtml } from '../lib/md.js'
import { transcriptText } from '../lib/transcript_export.js'

export function Preview() {
  const m = useMeeting()
  const [data, setData] = useState(null)
  const [space, setSpace] = useState('')
  const [spaces, setSpaces] = useState([])
  const [saved, setSaved] = useState(false)
  const [discard, setDiscard] = useState(false)
  const [title, setTitle] = useState('')
  const [word, setWord] = useState('')          // Việc 13: '' | 'Writing Word MoM…' | result message
  const mid = m.id
  useEffect(() => {
    if (!mid) return
    api.invoke('daemon', { cmd: 'meeting', meeting_id: mid }).then((r) => { if (r.ok) { setData(r); setTitle(r.meeting.name); setSpace(r.meeting.space || '') } })
    api.invoke('daemon', { cmd: 'spaces' }).then((r) => r.ok && setSpaces(r.spaces))
  }, [mid])
  const mom = data?.notes?.find((n) => n.kind === 'mom')?.text || data?.notes?.filter((n) => n.kind === 'live').slice(-1)[0]?.text || ''
  const save = async () => {
    await api.invoke('daemon', { cmd: 'set_name', meeting_id: mid, name: title || 'Untitled Note' })
    await api.invoke('daemon', { cmd: 'set_space', meeting_id: mid, space: space || 'default' })
    await api.invoke('daemon', { cmd: 'save_note', meeting_id: mid, text: mom })
    setSaved(true)
  }
  const close = () => { if (saved || !mom) api.invoke('window:close', 'preview-window'); else setDiscard(true) }
  const exportWord = async () => { if (!data || word === 'Writing Word MoM…') return; setWord('Writing Word MoM…'); const r = await api.invoke('export:docx', { meetingId: mid, title: title || data.meeting.name }); setWord(r.ok ? (r.warn?.length ? 'Word MoM saved — check: ' + r.warn.join(', ') : 'Word MoM saved') : r.canceled ? '' : 'Word export failed: ' + r.error); if (r.ok || r.canceled) setTimeout(() => setWord(''), 4000) }
  const exportTranscript = async (ext) => { if (!data) return; await api.invoke('export:file', { title: data.meeting.name + ' - transcript', ext, text: transcriptText(data.meeting, data.segments, ext) }) }
  useEffect(() => api.on('ui', (c) => { if (c === 'save') save(); if (c === 'export-transcript:md') exportTranscript('md'); if (c === 'export-transcript:txt') exportTranscript('txt'); if (c === 'export-docx') exportWord(); if (c === 'done') api.invoke('window:close', 'preview-window'); if (c === 'open-library') { api.invoke('window:open', 'library'); api.invoke('window:close', 'preview-window') } }), [mid, title, space, mom, data])
  return (
    <div className="preview">
      <div className="pv-head">
        <button className="icon-btn" title="Back" onClick={() => api.invoke('window:open', 'meeting-overlay')}>‹</button>
        {!saved && <button className="btn-accent" onClick={save} disabled={!data}>Save</button>}
        <input className="pv-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Untitled Note" />
        <span className="tmpl glow">General ✦</span>
        <button className="icon-btn" title="Export MoM in the company Word form (.docx)" onClick={exportWord} disabled={!data || word === 'Writing Word MoM…'}>W</button>
        <button className="icon-btn" title="Export original transcript (.md)" onClick={() => exportTranscript('md')}>⇩</button>
        <button className="icon-btn" title="Close" onClick={close}>✕</button>
      </div>
      {word && <div className="muted small pad-x">{word}</div>}
      {!saved ? (
        <div className="pv-space">
          <div className="label">CHOOSE A SPACE TO SAVE THIS NOTE</div>
          <input className="space-pick" list="spaces" value={space} onChange={(e) => setSpace(e.target.value)} placeholder="Pick or create one" />
          <datalist id="spaces">{spaces.map((s) => <option key={s} value={s} />)}</datalist>
        </div>
      ) : (
        <div className="pv-space saved-row"><span>Saved</span><span className="grow" /><button className="btn-ghost" onClick={() => api.invoke('window:close', 'preview-window')}>Done</button><button className="btn-accent" onClick={() => { api.invoke('window:open', 'library'); api.invoke('window:close', 'preview-window') }}>Open in library</button></div>
      )}
      <div className="pv-body prose" dangerouslySetInnerHTML={{ __html: data ? (mom ? mdToHtml(mom) : '<p class="muted">No notes were produced for this meeting.</p>') : '<p class="muted">Loading…</p>' }} />
      {discard && (
        <div className="veil"><div className="dialog"><h3>You'll lose this note</h3><div className="row"><button className="btn-accent" onClick={() => setDiscard(false)}>Keep editing</button><button className="btn-ghost" onClick={() => api.invoke('window:close', 'preview-window')}>Discard note</button></div></div></div>
      )}
    </div>
  )
}
