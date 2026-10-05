// Toolbar modelled on the reference app. Only the logo differs (letter M instead of the K).
// Window 88x88 collapsed / 88x254 expanded; hover enter 200 ms / leave 80 ms / stabilization 300 ms; the window snaps to size, the buttons spring in (CSS).
// Record states: idle = logo + 7 dots (55x55) · idle+hover = 23 px white/80 dot in a 1 px #C7DA35 ring ·
// active = logo #C7DA35 + 7 bars at 85 % · active+hover = bars only (viewBox "0 43 51 16", amplitude .8). Bars follow
// useWaveformAnimation: base .3-.5 + sin(t*speed+phase)*amp, clamped .15-.85, frozen when paused.
// Drag: threshold 3 px, click valid < 5 px. The mic capture pipeline lives in this (never destroyed) window.
import React, { useEffect, useRef, useState } from 'react'
import { api, useMeeting } from '../index.jsx'
import { startCapture, defaultOutputIsHeadphone } from '../audio/capture.js'

const ACCENT = '#C7DA35'
const BAR_PATHS = ['M25.8041 43.8176L25.8041 57.6419', 'M11.9797 46.1216L11.9797 55.3378', 'M39.6284 55.3378L39.6284 46.1216', 'M16.5879 47.2736L16.5878 54.1858', 'M35.0203 54.1858L35.0203 47.2736', 'M21.1959 46.1216L21.1959 55.3378', 'M30.4122 55.3378L30.4122 46.1216']
const DOT_X = [25.8041, 11.9797, 39.6284, 16.5879, 35.0203, 21.1959, 30.4122]

// LogoIcon: the reference app draws its K in viewBox 0 0 51 63 spanning x 10-45, y 8-36; Middy draws the M ring in the same box.
export function LogoM({ color = 'white', viewBox = '0 0 51 63', className }) {
  return (
    <svg className={className} viewBox={viewBox} fill="none" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">
      <circle cx="25.5" cy="22.5" r="14" stroke={color} strokeWidth="2.2" />
      <text x="25.5" y="22.8" fill={color} fontFamily="Inter, -apple-system, BlinkMacSystemFont, sans-serif" fontWeight="800" fontSize="15" textAnchor="middle" dominantBaseline="central">M</text>
    </svg>
  )
}
function StaticDots({ color = 'white' }) {                                  // StaticWaveformIcon : 7 squares 3x3 at y 50.7
  return <svg viewBox="0 0 51 63" fill="none" preserveAspectRatio="none">{DOT_X.map((x, i) => <path key={i} d={`M${x} 49.2L${x} 52.2`} stroke={color} strokeWidth="3" strokeLinecap="butt" />)}</svg>
}
function useWaveform(isRecording) {                                         // useWaveformAnimation
  const vals = useRef(Array(7).fill(0.4))
  const cfg = useRef(BAR_PATHS.map(() => ({ phase: Math.random() * Math.PI * 2, speed: 0.002 + Math.random() * 0.004, base: 0.3 + Math.random() * 0.2, amp: 0.2 + Math.random() * 0.2 })))
  const subs = useRef(new Set())
  const kick = useRef(() => {})
  useEffect(() => {
    if (!isRecording) return
    // Việc 20: the toolbar is hidden during a meeting, and every window runs with backgroundThrottling off (mic capture), so this
    // loop drew 60 frames/s for nothing all meeting long. It now runs only while the window is visible and bars are on screen.
    let id = null; const t0 = performance.now()
    const tick = (now) => {
      id = null
      if (document.hidden || !subs.current.size) return
      const t = now - t0; cfg.current.forEach((c, i) => { vals.current[i] = Math.max(0.15, Math.min(0.85, c.base + Math.sin(t * c.speed + c.phase) * c.amp)) }); subs.current.forEach((fn) => fn(vals.current))
      id = requestAnimationFrame(tick)
    }
    kick.current = () => { if (id === null && !document.hidden && subs.current.size) id = requestAnimationFrame(tick) }
    kick.current(); document.addEventListener('visibilitychange', kick.current)
    return () => { if (id !== null) cancelAnimationFrame(id); document.removeEventListener('visibilitychange', kick.current); kick.current = () => {} }
  }, [isRecording])
  return { vals, subs, kick }
}
function Waveform({ wave, color, viewBox = '0 0 51 63', amplitude = 1 }) {  // WaveformIcon : 7 strokes width 3 round, scaleY from the shared values
  const refs = useRef([])
  useEffect(() => { const apply = (v) => refs.current.forEach((p, i) => { if (p) p.style.transform = `scaleY(${v[i] * amplitude})` }); apply(wave.vals.current); wave.subs.current.add(apply); wave.kick.current(); return () => wave.subs.current.delete(apply) }, [wave, amplitude])
  return <svg className="wave" viewBox={viewBox} fill="none" preserveAspectRatio="none">{BAR_PATHS.map((d, i) => <path key={i} ref={(el) => { refs.current[i] = el }} d={d} stroke={color} strokeWidth="3" strokeLinecap="round" />)}</svg>
}

// AnimatePresence mode "wait": exiting content fades .3 s (idle logo exits instantly), then the next content mounts.
const EXIT_MS = { 'idle-logo': 0, 'idle-dot': 300, 'active-logo': 300, 'active-wave': 300 }
const FADE_IN = { 'idle-dot': true, 'active-logo': true }
function useWaitSwitch(target) {
  const [shown, setShown] = useState(target)
  const [exiting, setExiting] = useState(false)
  useEffect(() => {
    if (shown === target) return
    const ms = EXIT_MS[shown]
    if (!ms) { setShown(target); return }
    setExiting(true)
    const t = setTimeout(() => { setShown(target); setExiting(false) }, ms)
    return () => clearTimeout(t)
  }, [target, shown])
  return [shown, exiting]
}

export function Toolbar() {
  const m = useMeeting()
  const [hover, setHover] = useState(false)
  const [armed, setArmed] = useState(false)          // side buttons accept clicks 300 ms after expanding
  const [dragging, setDragging] = useState(false)
  const timer = useRef(null)
  const expandedAt = useRef(0)
  const inside = useRef(false)
  const cap = useRef(null)
  const drag = useRef(null)
  const starting = useRef(false)

  // window snaps to the new size; the content animates in CSS
  useEffect(() => {
    api.invoke('window:size', hover ? 'toolbar-expanded' : 'toolbar')
    if (hover) { const t = setTimeout(() => setArmed(true), 300); return () => clearTimeout(t) }
    setArmed(false)
  }, [hover])
  // frame-rate probe for the review (--test-expand): count rAF ticks over the 506 ms spring
  useEffect(() => { let n = 0; const t0 = performance.now(); let id; const tick = () => { n++; if (performance.now() - t0 < 506) id = requestAnimationFrame(tick); else api.invoke('daemon-log', { tb_anim: { expanded: hover, frames: n, ms: Math.round(performance.now() - t0), fps: Math.round(n / ((performance.now() - t0) / 1000)) } }).catch(() => {}) }; id = requestAnimationFrame(tick); return () => cancelAnimationFrame(id) }, [hover])

  // useExpandOnHover: enter 200 ms, leave 80 ms, at least 300 ms expanded; a leave with the button held waits for mouseup
  const enter = () => { inside.current = true; clearTimeout(timer.current); timer.current = setTimeout(() => { if (inside.current) { expandedAt.current = Date.now(); setHover(true) } }, 200) }
  const leave = (e) => {
    inside.current = false; clearTimeout(timer.current)
    if (e.buttons === 1) { window.addEventListener('mouseup', () => { if (!inside.current) setHover(false) }, { once: true }); return }
    const remaining = 300 - (Date.now() - expandedAt.current)
    timer.current = setTimeout(() => { if (!inside.current) setHover(false) }, Math.max(80, remaining))
  }
  useEffect(() => api.on('ui', (c) => { if (c === 'tb-expand') { expandedAt.current = Date.now(); setHover(true) } if (c === 'tb-collapse') setHover(false) }), [])

  // capture follows the meeting state: start when the daemon starts, stop on done
  useEffect(() => {
    if ((m.state === 'starting' || m.state === 'recording') && !cap.current && !starting.current) {
      starting.current = true
      ;(async () => {
        const forced = await api.invoke('output-device')                       // test flags (--force-aec / --force-headphone) win
        const out = forced.label === 'forced' ? forced : await defaultOutputIsHeadphone()
        api.invoke('daemon-log', { outputDevice: out }).catch(() => {})
        const settings = await api.invoke('settings:get')
        try {
          const c = await startCapture({ systemAudioMode: m.systemAudioMode, headphone: out.isHeadphoneOutput, deviceId: settings.micDeviceId,
            onDiag: (d) => api.invoke('daemon-log', d).catch(() => {}),
            onStatus: (s) => api.invoke('daemon-log', { aecStatus: s }).catch(() => {}),
            onEvent: (ev) => api.invoke('daemon-log', { capture: ev }).catch(() => {}) })
          cap.current = c
        } catch (e) { api.invoke('daemon-log', { captureError: String(e) }).catch(() => {}) }
        starting.current = false
      })()
    }
    if (!['starting', 'recording', 'finishing'].includes(m.state) && cap.current) { cap.current.stop(); cap.current = null }
    return () => {}
  }, [m.state])
  useEffect(() => api.on('event', (ev) => { if (ev.type === 'pause' && cap.current) cap.current.pause(); if (ev.type === 'resume' && cap.current) cap.current.resume() }), [])
  useEffect(() => api.on('capture-control', (c) => { if (!cap.current) return; if (c === 'pause') cap.current.pause(); if (c === 'resume') cap.current.resume(); if (c === 'rebuild') cap.current.rebuild('watchdog'); if (c === 'kill-track') cap.current.killTrackForTest() }), [])
  const [net, setNet] = useState(null)
  const [sc, setSc] = useState(null)                                   // Việc 16: shortcut shown on the Record tooltip
  useEffect(() => { api.invoke('shortcut:get').then(setSc); return api.on('shortcut', setSc) }, [])
  const scTip = sc?.ok ? ` (${sc.label})` : ''
  useEffect(() => { api.invoke('net:status').then(setNet); return api.on('net', setNet) }, [])

  const active = ['starting', 'recording', 'finishing'].includes(m.state)     // the reference app isActive = meetingPhase !== "idle"
  const isRecording = m.state === 'recording' && !m.paused                      // bars move + turn #C7DA35 only while recording
  const wave = useWaveform(isRecording)
  const onDown = (e) => { drag.current = { x: e.screenX, y: e.screenY, moved: 0 } }
  const onMove = (e) => { const d = drag.current; if (!d) return; const dx = e.screenX - d.x, dy = e.screenY - d.y; d.moved = Math.max(d.moved, Math.hypot(dx, dy)); if (d.moved >= 3) { setDragging(true); api.invoke('window:move', { dx, dy }); d.x = e.screenX; d.y = e.screenY } }
  const onUp = (fn) => (e) => { const d = drag.current; drag.current = null; setDragging(false); if (d && d.moved < 5 && fn) fn(e) }

  const netOk = !!(net && net.blocked)
  const record = async () => { if (active) api.invoke('window:open', 'meeting-overlay'); else if (netOk) await api.invoke('meeting:start', {}); else api.invoke('window:open', 'settings') }
  const target = active ? (hover ? 'active-wave' : 'active-logo') : (hover ? 'idle-dot' : 'idle-logo')
  const [shown, exiting] = useWaitSwitch(target)
  const rcClass = 'rc' + (FADE_IN[shown] ? ' fade-in' : '') + (exiting ? ' exiting' : '')
  const barColor = isRecording ? ACCENT : 'white'
  return (
    <div className="tb-home">
      <div className="vibrancy-overlay" aria-hidden="true" />
      <div className={'tb-shell' + (hover ? ' expanded' : '') + (dragging ? ' dragging' : '')} onMouseEnter={enter} onMouseLeave={leave} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp()}>
        {hover && <div className={'tb-side' + (armed ? ' armed' : '')}>
          <button className="tb-btn" title="Settings" onMouseUp={onUp(() => api.invoke('window:open', 'settings'))}>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915" /><circle cx="12" cy="12" r="3" /></svg>
          </button>
        </div>}
        {hover && <div className={'tb-side' + (armed ? ' armed' : '')}>
          <button className="tb-btn" title="My spaces" onMouseUp={onUp(() => api.invoke('window:open', 'library'))}>
            <LogoM className="logo-svg" viewBox="0 0 51 40" />
          </button>
        </div>}
        <div className={'tb-record-wrap' + (!netOk && !active ? ' disabled' : '')}>
          <button className="tb-record" title={(active ? (isRecording ? 'Now recording' : 'Recording paused') + (scTip && ' — stop with ' + sc.label) : netOk ? 'Start meeting' + scTip : 'Network block not verified — see Settings')} onMouseUp={onUp(record)}>
            {shown === 'idle-logo' && <div className={rcClass}><div className="rc-box" style={{ width: 55, height: 55 }}><LogoM /><StaticDots /></div></div>}
            {shown === 'idle-dot' && <div className={rcClass}><div className="rec-ring"><div className="rec-dot" /></div></div>}
            {shown === 'active-logo' && <div className={rcClass}><div className="rc-box" style={{ width: '85%', height: '85%' }}><LogoM color={ACCENT} /><Waveform wave={wave} color={barColor} amplitude={1.2} /></div></div>}
            {shown === 'active-wave' && <div className={rcClass}><Waveform wave={wave} color={barColor} viewBox="0 43 51 16" amplitude={0.8} /></div>}
          </button>
        </div>
      </div>
    </div>
  )
}
