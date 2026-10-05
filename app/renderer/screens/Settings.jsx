// Order: MICROPHONE · MEETINGS · PERMISSIONS · SHORTCUT · NETWORK
// Lỗi 10 (28/09): the transcription language moved to the meeting overlay; auto-translate removed ("không làm dịch").
// plus NETWORK (design 1.9 "connection check": count of blocked requests) - a local-only addition.
import React, { useEffect, useState } from 'react'
import { api, useSettings } from '../index.jsx'

// Lỗi 9b: optional Accessibility permission, used ONLY to read whether you are muted in Teams/Zoom (never clicks anything)
function AxCard() {
  const [ax, setAx] = useState(null)
  useEffect(() => { const f = () => api.invoke('ax:status').then(setAx); f(); const t = setInterval(f, 2000); return () => clearInterval(t) }, [])
  return (
    <div className={'card' + (ax && !ax.trusted ? '' : '')}>
      <div>Accessibility (optional) {ax ? (ax.trusted ? '✓ allowed' : '— not allowed') : '…'}</div>
      <div className="muted small">Lets Middy read whether you are muted in Microsoft Teams or Zoom. It only reads the mic button, never clicks anything. When you are unmuted there, Middy always transcribes your mic; when you are muted, the Mic on/off switch in the meeting window decides.</div>
      {ax && !ax.trusted && <div className="row" style={{ marginTop: 8 }}><button className="btn-accent" onClick={() => api.invoke('ax:request').then(setAx)}>Allow…</button><button className="btn-ghost" onClick={() => api.invoke('ax:open-settings')}>Open Privacy settings</button></div>}
    </div>
  )
}

// Việc 16: the system-wide Start/Stop shortcut. "Change" records the next key combination (Esc cancels); a combination another
// app already owns is reported here, never ignored silently.
function ShortcutCard() {
  const [sc, setSc] = useState(null)
  const [rec, setRec] = useState(false)
  const [msg, setMsg] = useState('')
  useEffect(() => { api.invoke('shortcut:get').then(setSc); return api.on('shortcut', setSc) }, [])
  const stopRec = () => { setRec(false); api.invoke('shortcut:suspend', false).then(setSc) }
  const onKey = async (e) => {
    e.preventDefault()
    if (e.code === 'Escape') { stopRec(); return }
    if (/^(Shift|Control|Alt|Meta)/.test(e.code)) return                   // wait for the non-modifier key
    const r = await api.invoke('shortcut:record', { code: e.code, metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, shiftKey: e.shiftKey })
    setRec(false); setSc(r); setMsg(r.error ? (r.rejected ? r.error + ' — kept ' + r.label : r.error) : 'Saved')
    if (!r.rejected) api.invoke('shortcut:suspend', false).then(setSc)
  }
  const start = () => { setMsg(''); setRec(true); api.invoke('shortcut:suspend', true) }
  return (
    <div className={'card' + (sc && !sc.ok ? ' bad' : '')}>
      <div className="row"><span className="grow">Start / stop recording</span>
        {rec ? <input autoFocus className="field" style={{ width: 150 }} readOnly placeholder="Press keys… (Esc)" onKeyDown={onKey} onBlur={stopRec} />
          : <><b>{sc ? sc.label : '…'}</b><button className="btn-ghost" onClick={start}>Change</button><button className="btn-ghost" onClick={() => { setMsg(''); api.invoke('shortcut:reset').then((r) => { setSc(r); setMsg(r.error || 'Saved') }) }}>Default</button></>}
      </div>
      <div className="muted small">Works from any app. Not in a meeting: starts one. In a meeting: stops it (the notes finish in the background).</div>
      {sc && !sc.ok && <div className="danger small">{sc.label} is not active: {sc.error}</div>}
      {msg && <div className={'small ' + (/used|Use |Add /.test(msg) ? 'danger' : 'muted')}>{msg}</div>}
    </div>
  )
}

export function Settings() {
  const [s, set] = useSettings()
  const [net, setNet] = useState(null)
  const [mics, setMics] = useState([])
  useEffect(() => { api.invoke('net:status').then(setNet); const off = api.on('net', setNet); navigator.mediaDevices.enumerateDevices().then((d) => setMics(d.filter((x) => x.kind === 'audioinput'))).catch(() => {}); return off }, [])
  if (!s) return null
  return (
    <div className="settings">
      <div className="st-head"><span className="logo-m small">M</span><span className="label">SETTINGS</span><span className="grow" /><button className="icon-btn" onClick={() => api.invoke('window:close', 'settings')}>✕</button></div>
      <div className="st-body">
        <div className="label">MICROPHONE</div>
        <select className="field" value={s.micDeviceId} onChange={(e) => set({ micDeviceId: e.target.value })}><option value="default">System default</option>{mics.map((d) => <option key={d.deviceId} value={d.deviceId}>{d.label || d.deviceId.slice(0, 8)}</option>)}</select>
        <hr />
        <div className="label">MEETINGS</div>
        <label className="card row">
          <span>📷</span>
          <span className="grow"><div>Screen Capture</div><div className="muted small">Capture the shared screen when the slide changes; text is read on this Mac (Apple Vision) and used as context. Nothing leaves the machine.</div></span>
          <input type="checkbox" className="switch" checked={!!s.screenCapture} onChange={(e) => set({ screenCapture: e.target.checked })} />
        </label>
        <label className="card row">
          <span>✍️</span>
          <span className="grow"><div>Minutes by Claude Code</div><div className="muted small">Off: the local model writes the minutes after Stop (loaded only then, about 8 GB; optional download: install.sh --with-local-llm). On: no local model at all; Claude Code reads the meeting through the Middy MCP server and writes the minutes. The transcript reaches Claude only when you call its tools.</div></span>
          <input type="checkbox" className="switch" checked={s.summarizer === 'claude'} onChange={(e) => set({ summarizer: e.target.checked ? 'claude' : 'local' })} />
        </label>
        <hr />
        <div className="label">PERMISSIONS</div>
        <div className="card"><div>Microphone</div><div className="muted small">Asked by macOS on the first meeting.</div></div>
        <AxCard />
        <div className="card"><div>System Audio Recording</div><div className="muted small">Required to hear the other side (audiotee). macOS ≥ 14.2.</div></div>
        <hr />
        <div className="label">SHORTCUT</div>
        <ShortcutCard />
        <hr />
        <div className="label">NETWORK</div>
        <div className={'card' + (net && net.done && !net.blocked ? ' bad' : '')}>
          <div>Connection check {net && net.done ? (net.blocked ? '✓ blocked at kernel level' : '✗ NOT blocked') : '…'}</div>
          <div className="muted small">Self-test at launch (this process): TCP 1.1.1.1:443 → <b>{net?.tcp || '…'}</b> · DNS apple.com → <b>{net?.dns || '…'}</b> · sandbox-exec {net?.sandbox ? 'on' : 'OFF'}. Requests cancelled by the app since launch: <b>{net ? net.blockedRequests : '…'}</b>{net?.last ? ' · last: ' + net.last : ''}. Workers run under the same no-network sandbox.{net && net.done && !net.blocked ? ' Recording is disabled until the block is verified.' : ''}</div>
        </div>
      </div>
    </div>
  )
}
