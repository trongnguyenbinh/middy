// Meeting overlay: top bar (End meeting + MM:SS | space square + Hide window), Note panel (#292b31, r12): header (waveform, pause,
// AI auto summary toggle, editable title, tabs Transcript / Notes), Transcript bubbles (mic = "You", right aligned, no time),
// Notes = TipTap (read-only while auto summary writes), overlays: "End this meeting?", "No activity — end meeting?", reconnecting.
import React, { useEffect, useRef, useState } from 'react'
import { api, useMeeting, useSettings } from '../index.jsx'
import { applyWindow, bubbles, avatarColor } from '../lib/transcript.js'
import { NoteEditor } from './NoteEditor.jsx'
import { mdToHtml } from '../lib/md.js'

const mmss = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0') }
const TEMPLATES = ['General', 'Concise Recap', 'Daily Standup', 'Customer Call']

// Lỗi 10: same list the daemon accepts (proto/core.py LANGUAGES); short codes keep the header compact
const LANG_CODES = { English: 'EN', Vietnamese: 'VI', Chinese: 'ZH', Japanese: 'JA', Korean: 'KO', German: 'DE', French: 'FR', Spanish: 'ES' }

// Việc 20: the MM:SS clock re-renders alone (it used to re-render the whole overlay, transcript list included, every 500 ms)
function Clock({ readyAt }) {
  const [c, setC] = useState('00:00')
  useEffect(() => { const f = () => setC(mmss(readyAt ? Date.now() - readyAt : 0)); f(); const t = setInterval(f, 1000); return () => clearInterval(t) }, [readyAt])
  return <span className="clock">{c}</span>
}

export function Overlay() {
  const m = useMeeting()
  const [tab, setTab] = useState('transcript')
  const [entries, setEntries] = useState([])
  const [interim, setInterim] = useState({})
  const [note, setNote] = useState('')                 // live notes markdown (AI auto summary)
  const [auto, setAuto] = useState(true)
  const [paused, setPaused] = useState(false)
  const [title, setTitle] = useState(m.title || 'Untitled Note')
  const [confirm, setConfirm] = useState(false)
  const [idle, setIdle] = useState(null)
  const [st, setSt] = useSettings()
  const micOn = !st || st.micInput !== false
  const [appMic, setAppMic] = useState(null)                                    // Lỗi 9b: meeting app's own mic state (read-only)
  useEffect(() => { api.invoke('mic-state:get').then(setAppMic); return api.on('mic-state', setAppMic) }, [])
  const [langOpen, setLangOpen] = useState(false)                               // Lỗi 10: transcription language, moved here from Settings
  const lang = m.language || st?.language || 'English'
  const pickLang = (l) => { setLangOpen(false); if (l !== lang) api.invoke('meeting:language', l) }                                     // Lỗi 9: mic-input switch
  const [reminder, setReminderState] = useState(null)                          // Lỗi 7 auto-end prompt, driven by main
  const [nowMs, setNowMs] = useState(Date.now())
  useEffect(() => { api.invoke('reminder:get').then(setReminderState); return api.on('reminder', setReminderState) }, [])
  useEffect(() => { if (!reminder) return; const t = setInterval(() => setNowMs(Date.now()), 250); return () => clearInterval(t) }, [reminder])
  const [writing, setWriting] = useState(false)
  const [warn, setWarn] = useState(null)
  const [_userNote, setUserNote] = useState('')    // never read: notes typed here are not saved (open question in the CI PR)
  const listRef = useRef(null)
  // Việc 17: "Ask anything": questions about the meeting so far, answered by the meeting's own local Gemma,
  // streamed into the ✦ tab and saved with the meeting (Library shows them)
  const [asks, setAsks] = useState([])                 // [{key, id, q, error}]
  const [answers, setAnswers] = useState({})           // id -> {text, done}  (deltas may arrive before the id reply does)
  const [askText, setAskText] = useState('')
  const askRef = useRef(null)
  const canAsk = m.state === 'recording'
  const sendAsk = async () => {
    const q = askText.trim(); if (!q || !canAsk) return
    const key = Date.now(); setAskText(''); setTab('ask'); setAsks((a) => [...a, { key, q }])
    const r = await api.invoke('meeting:ask', q)
    setAsks((a) => a.map((x) => x.key === key ? (r.ok ? { ...x, id: r.id } : { ...x, error: r.error || 'Could not ask' }) : x))
  }

  useEffect(() => { if (m.title) setTitle(m.title) }, [m.title])
  useEffect(() => api.on('event', (ev) => {
    if (ev.type === 'transcript_window') {
      setEntries((e) => { if (!ev.settled && e.length < (ev.windowOffset || 0)) { api.invoke('daemon', { cmd: 'snapshot' }).then((r) => { if (r.ok && r.snapshot) setEntries(applyWindow([], r.snapshot)) }); return e }   // prefix missing (rnd M2 item 7) -> resync
        return applyWindow(e, ev) })
      if (ev.interim) setInterim(ev.interim)
    }
    else if (ev.type === 'warn' && ev.where === 'mic_silent') setWarn('Microphone stopped delivering audio — reconnecting…')
    else if (ev.type === 'warn' && ev.where === 'mic_back') setWarn(null)
    else if (ev.type === 'capture') {
      if (ev.type === 'capture' && ['mic-ended', 'mic-rebuild-failed'].includes(ev.captureType)) setWarn('Microphone disconnected — reconnecting…')
      if (ev.captureType === 'mic-rebuilt') { setWarn('Microphone reconnected: ' + (ev.device || '')); setTimeout(() => setWarn(null), 4000) }
      if (ev.captureType === 'output-changed') { setWarn((ev.headphone ? 'Headphones' : 'Speakers') + ' detected: echo cancellation ' + (ev.headphone ? 'off' : 'on')); setTimeout(() => setWarn(null), 4000) }
    }
    else if (ev.type === 'transcript' && !ev.is_final) setInterim((i) => ({ ...i, [ev.source]: ev.text }))
    else if (ev.type === 'note' && ev.kind === 'live' && ev.kept) { setNote(ev.text); setWriting(true); setTimeout(() => setWriting(false), 1200) }
    else if (ev.type === 'idle') setIdle(ev.silence_s)
    else if (ev.type === 'ask_delta') setAnswers((s) => ({ ...s, [ev.id]: { text: (s[ev.id]?.text || '') + ev.text, done: false } }))
    else if (ev.type === 'ask_done') setAnswers((s) => ({ ...s, [ev.id]: { text: ev.text, done: true } }))
    else if (ev.type === 'resync') api.invoke('daemon', { cmd: 'snapshot' }).then((r) => { if (r.ok && r.snapshot) setEntries(applyWindow([], r.snapshot)) })
  }), [])
  useEffect(() => { api.invoke('daemon', { cmd: 'snapshot' }).then((r) => { if (r.ok && r.snapshot) setEntries(applyWindow([], r.snapshot)) }).catch(() => {}) }, [])   // mount mid-meeting: full prefix (rnd M2 item 7)
  useEffect(() => { const el = listRef.current; if (el) el.scrollTop = el.scrollHeight }, [entries, interim])
  useEffect(() => { const el = askRef.current; if (el) el.scrollTop = el.scrollHeight }, [asks, answers])
  useEffect(() => api.on('ui', (c) => { if (c === 'tab:notes') setTab('notes'); if (c === 'tab:transcript') setTab('transcript'); if (c === 'confirm') setConfirm(true); if (c === 'confirm:off') setConfirm(false) }), [])

  const end = async () => { setConfirm(false); await api.invoke('meeting:stop') }
  const togglePause = () => { const p = !paused; setPaused(p); api.invoke('toolbar:capture', p ? 'pause' : 'resume') }
  const rows = bubbles(entries, interim)
  const finishing = m.state === 'finishing' || m.state === 'done'
  return (
    <div className="overlay">
      <div className="ov-top">
        <button className="btn-accent" onClick={() => setConfirm(true)} disabled={finishing}>{m.state === 'finishing' ? 'Saving...' : m.state === 'done' ? 'Preparing...' : 'End meeting'}</button>
        <Clock readyAt={m.readyAt} />
        <span className="grow" />
        <span className="space-sq" title={'Space: ' + (m.space || 'default')}>{(m.space || 'D')[0].toUpperCase()}</span>
        <button className="icon-btn" title="Hide window" onClick={() => api.invoke('window:hide')}>⌄</button>
      </div>
      {warn && <div className="warnbar">⚠ {warn}</div>}
      {!micOn && !finishing && (appMic?.appMicOn === true
        ? <div className="micauto-bar">{appMic.app} mic is on — Middy transcribes your mic until you mute in {appMic.app}.</div>
        : <div className="micoff-bar">Mic input is off — only the other side is being transcribed.</div>)}
      <div className="panel note-panel">
        <div className="np-head">
          <span className={'bars small' + (paused ? '' : ' live')}>{Array.from({ length: 5 }).map((_, i) => <i key={i} style={{ animationDelay: (i * 0.13) + 's' }} />)}</span>
          <button className="icon-btn" title={paused ? 'Resume' : 'Pause'} onClick={togglePause}>{paused ? '▶' : '❚❚'}</button>
          <button className={'mic-toggle ' + (micOn ? 'on' : 'off')} aria-pressed={micOn} onClick={() => setSt({ micInput: !micOn })}
            title={micOn ? 'Mic input ON: your microphone is transcribed. Click to stop using it (e.g. while muted in Teams/Zoom).' : 'Mic input OFF: nothing from your microphone is used. Only the other side is transcribed. Click to turn it on.'}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19v3" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><rect x="9" y="2" width="6" height="13" rx="3" />{!micOn && <path d="M2 2 22 22" />}</svg>
            {micOn ? 'Mic on' : 'Mic off'}
          </button>
          <div className="lang-pick">
            <button className="lang-btn" onClick={() => setLangOpen(!langOpen)} aria-haspopup="listbox" aria-expanded={langOpen}
              title={'Transcription language: ' + lang + '. A change applies to speech from that moment on; what is already transcribed stays as it is.'}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" /><path d="M2 12h20" /></svg>
              {LANG_CODES[lang] || lang}
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={langOpen ? 'rot' : ''}><path d="m6 9 6 6 6-6" /></svg>
            </button>
            {langOpen && (
              <div className="lang-list" role="listbox">
                {Object.keys(LANG_CODES).map((l) => (
                  <button key={l} role="option" aria-selected={l === lang} className={l === lang ? 'sel' : ''} onClick={() => pickLang(l)}>
                    <span>{l}</span>{l === lang && <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>}
                  </button>
                ))}
              </div>
            )}
          </div>
          {tab === 'notes' && <button className={'chip' + (auto ? ' on' : '')} title="AI auto summary" onClick={() => setAuto(!auto)}>✦ AI auto summary</button>}
          <input className="np-title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => api.invoke('meeting:title', title || 'Untitled Note')} />
          <div className="tabs">
            <button className={tab === 'transcript' ? 'on' : ''} title="Transcript" onClick={() => setTab('transcript')}>≡</button>
            <button className={tab === 'notes' ? 'on' : ''} title="Notes" onClick={() => setTab('notes')}>▤</button>
            <button className={tab === 'ask' ? 'on' : ''} title="Ask" onClick={() => setTab('ask')}>✦</button>
          </div>
        </div>
        {tab === 'ask' ? (
          <div className="bubbles" ref={askRef}>
            {asks.length === 0 && <div className="muted center">Ask anything about this meeting so far. Answers are written on this Mac from the transcript and notes.</div>}
            {asks.map((x) => { const ans = x.id && answers[x.id]; return (
              <React.Fragment key={x.key}>
                <div className="turn mine"><div className="bubble">{x.q}</div></div>
                <div className="turn"><div className="who"><span className="avatar" style={{ background: '#C7DA35', color: '#111' }}>✦</span><span className="name">Middy</span></div>
                  {x.error ? <div className="bubble danger">{x.error}</div>
                    : ans?.text ? <div className="bubble prose" dangerouslySetInnerHTML={{ __html: mdToHtml(ans.text) + (ans.done ? '' : ' ▍') }} />
                    : <div className="bubble muted">Thinking…</div>}
                </div>
              </React.Fragment>) })}
          </div>
        ) : tab === 'transcript' ? (
          <div className="bubbles" ref={listRef}>
            {rows.length === 0 && <div className="muted center">Listening for speech...</div>}
            {rows.map((b) => (
              <div key={b.id} className={'turn' + (b.source === 'mic' ? ' mine' : '') + (b.state === 'listening' ? ' interim' : '')}>
                <div className="who"><span className="avatar" style={{ background: avatarColor(b.speaker) }}>{b.speaker[0]}</span><span className="name">{b.speaker}</span></div>
                <div className="bubble">{b.text}</div>
              </div>
            ))}
          </div>
        ) : (
          <div className="notes-tab">
            <div className="notes-tools">
              <select className="tmpl" defaultValue="General" title="Notes Templates">{TEMPLATES.map((t) => <option key={t}>{t}</option>)}</select>
              {auto && <span className="muted small">AI is writing · editor read-only</span>}
            </div>
            <div className={writing ? 'shimmer' : ''}>
              <NoteEditor html={auto ? mdToHtml(note) : undefined} readOnly={auto} placeholder="Type your notes here..." onChange={setUserNote} />
            </div>
          </div>
        )}
      </div>
      <div className="chatbar">
        <span className="muted">✦</span>
        <input className="ask-input grow" value={askText} disabled={!canAsk} placeholder={canAsk ? 'Ask anything...' : 'Ask anything... (once recording has started)'}
          onChange={(e) => setAskText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); sendAsk() } }} />
        <button className="icon-btn" title="Send" disabled={!canAsk || !askText.trim()} onClick={sendAsk}>➤</button>
      </div>
      {m.state === 'starting' && (       /* Lỗi 15: capture runs from the click, no blocking dialog */
        <div className="muted small pad-x">Preparing… recording is already being kept.</div>
      )}
      {confirm && (
        <div className="veil"><div className="dialog"><h3>End this meeting?</h3><div className="row"><button className="btn-accent" onClick={end}>End meeting</button><button className="btn-ghost" onClick={() => setConfirm(false)}>Keep going</button></div></div></div>
      )}
      {reminder && reminder.kind === 'end_meeting_no_activity' && (
        <div className="rm-veil" role="dialog" aria-modal="true" aria-label="End meeting reminder"><div className="rm-card">
          <h3>No activity — end meeting?</h3>
          <p>Your meeting app released the mic and nothing has been said for 20 seconds. Recording will auto-end in <b>{Math.max(0, Math.ceil((reminder.deadlineAt - nowMs) / 1000))}s</b>.</p>
          <div className="rm-row"><button className="rm-keep" onClick={() => api.invoke('reminder:keep')}>Keep recording</button><button className="rm-end" onClick={() => api.invoke('reminder:end')}>End meeting</button></div>
        </div></div>
      )}
      {idle !== null && !confirm && !reminder && (
        <div className="veil"><div className="dialog"><h3>No activity — end meeting?</h3><p className="muted">No speech for {Math.round(idle)} s.</p><div className="row"><button className="btn-accent" onClick={end}>End meeting</button><button className="btn-ghost" onClick={() => setIdle(null)}>Still in this meeting?</button></div></div></div>
      )}
    </div>
  )
}
