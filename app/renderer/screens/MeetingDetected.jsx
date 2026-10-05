// Lỗi 7 — "… meeting detected" popup, modelled on the reference app (window 300x100 top-right):
// vibrancy-overlay-dark · logo 32 px + "{App} meeting detected" (15 px medium) + X (16 px, white/50) · full-width "Start recording"
// (#C7DA35, black 14 px semibold, rounded 8, lucide video 18 px) · 5 px bar at the bottom shrinking 100% -> 0 in 30 s, linear.
import React, { useEffect, useState } from 'react'
import { api } from '../index.jsx'
import { LogoM } from './Toolbar.jsx'

const AUTO_DISMISS_S = 30                                                     // the reference app MEETING_DETECTED_AUTO_DISMISS_MS
export function MeetingDetected() {
  const [meeting, setMeeting] = useState(null)
  const [run, setRun] = useState(0)                                          // restarts the bar when a new detection arrives
  useEffect(() => api.on('meeting-detected-info', (m) => { setMeeting(m); setRun((r) => r + 1) }), [])
  useEffect(() => { api.invoke('meeting-detected:get').then((m) => { if (m) { setMeeting(m); setRun((r) => r + 1) } }) }, [])
  return (
    <div className="md-root">
      <div className="vibrancy-overlay-dark" aria-hidden="true" />
      <div className="md-body">
        <div className="md-top">
          <div className="md-row">
            <div className="md-logo"><LogoM /></div>
            <span className="md-title">{(meeting?.appLabel ?? 'Meeting') + ' meeting detected'}</span>
            <button type="button" className="md-x" onClick={() => api.invoke('meeting-detected:dismiss')}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
            </button>
          </div>
          <button type="button" className="md-start" onClick={() => api.invoke('meeting-detected:accept')}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5" /><rect x="2" y="6" width="14" height="12" rx="2" /></svg>
            {' Start recording'}
          </button>
        </div>
        {meeting && <div className="md-track" aria-hidden="true"><div key={meeting.sourceId + '-' + run} className="md-bar" style={{ animationDuration: AUTO_DISMISS_S + 's' }} /></div>}
      </div>
    </div>
  )
}
